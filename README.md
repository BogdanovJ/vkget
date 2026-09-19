# VKGET

Self-hosted VK/VKVideo subscription manager for k3s.

Features:
- one-off URLs
- playlist/channel subscriptions
- initial "last N" newest videos first
- future discovery with remembered history
- duration and stop-word filtering
- hard 720p ceiling
- H.264 High@L4.0 + AAC-LC stereo MP4 for Samsung TV playback
- one active download at a time
- randomized discovery/retry scheduling
- MariaDB persistence
- Telegram notifications
- subscription folder sorting
- `/mnt/downloads` host mount exposed as `/downloads`
- minimal 16-bit-inspired UI

## Storage

Kubernetes mounts the node's existing `/mnt/downloads` to `/downloads`.

vkget treats `/downloads` as ready when it is writable and marked as the real share. On a Kubernetes hostPath/PVC mount it writes `.vkget-share` itself on first check. If that marker is missing and `/downloads` is not a mount (the empty directory from the image), downloads stay paused so files are not written into ephemeral container storage.

Subscription downloads go in one folder named after the subscription:

`/downloads/{subscription}/{YYYY-MM-DD} - {title} [{id}].mp4`

Files are remuxed or transcoded to a Samsung-safe MP4: H.264 High@L4.0, 8-bit 4:2:0, even dimensions up to 1280×720, AAC-LC stereo (48 kHz), `avc1` + faststart. HE-AAC, surround, `avc3`, high level, odd sizes, and VP9/AV1/Opus sources are converted. Existing library files that fail those checks are re-encoded when the downloader is idle.

One-off downloads use the uploader name, or `_single`. Placeholder leftovers (`Subscription`, `Unknown`, `NA`) are never used as folder names. If the upload date is unknown, the date prefix is omitted instead of writing `NA`.

## Database

Use the existing MariaDB instance with a dedicated database/user named `vkget`.

## Cookies

Create a Kubernetes Secret containing a working Netscape-format `cookies.txt`.
It is mounted read-only at `/config/cookies.txt`.

## VK hosts

Playlist and channel **scans** use `vkvideo.ru` (including older rows stored as `vk.com/playlist/...`). Video **downloads** use `vk.com`. If a download fails, vkget retries the other host (`vk.com` ↔ `vkvideo.ru`). A failed vkvideo scan also falls back to `vk.com`.

Pasted URLs keep their host when saved. A `vkvideo.ru` subscription stays `vkvideo.ru` in the UI.

## Bot protection (optional FlareSolverr)

VK/vkvideo sometimes show a JavaScript/cookie challenge similar to Cloudflare. [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) drives a browser, tries to clear that challenge, and returns cookies.

FlareSolverr is expected in-cluster at `http://flaresolverr.flaresolverr.svc.cluster.local:8191`. That is the `FLARESOLVERR_URL` default; override it if needed, or set it empty to disable.

When a scan or download hits 403 / challenge-style errors, vkget asks FlareSolverr for cookies for that URL and retries yt-dlp with those cookies plus any existing `cookies.txt`.

This is a best-effort hook, not a guarantee. FlareSolverr is built for Cloudflare-like challenges. VK may use a different wall. A real browser session can still help when the block is cookie/JS based. It will not replace a logged-in `cookies.txt`, solve interactive CAPTCHAs, or lift rate limits.

Example sidecar/service (documentation only — not part of the bundled manifests):

```bash
docker run -d --name flaresolverr -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest
```

On k3s, run FlareSolverr as a Deployment/Service; the ConfigMap already points at that in-cluster URL. No sidecar is required.

## Optional VPN routing

vkget supports optional VPN routing for yt-dlp downloads.

Supported VPN types:

- WireGuard
- OpenVPN

VPN configuration is managed from the web UI. When VPN is disabled, vkget downloads exactly as normal. When VPN is enabled, yt-dlp can use the selected VPN profile. If "Fallback to direct" is enabled and the VPN cannot connect or the VPN-routed download fails, vkget automatically retries using its normal direct connection.

Only yt-dlp download attempts use a SOCKS proxy on a dedicated `vkget-vpn-gateway` Deployment. That gateway is the only container with `NET_ADMIN` and `/dev/net/tun`. The vkget app container stays unprivileged. The gateway Service has no Ingress.

The UI `/vpn` page is the master switch and profile manager. Runtime ON/OFF is stored in the app database (`app_state`) and overrides the `VPN_ENABLED` environment default.

| Variable | Default | Meaning |
|---|---|---|
| `VPN_ENABLED` | `false` | Initial master switch if the UI has not saved a value |
| `VPN_FALLBACK_TO_DIRECT` | `true` | Retry the original direct downloader if VPN fails |
| `VPN_GATEWAY_URL` | in-cluster gateway `:8081` | Control API (`/connect`, `/disconnect`, `/status`) |
| `VPN_PROXY_URL` | in-cluster SOCKS `:1080` | Passed to yt-dlp as `--proxy` only while a tunnel is in use |
| `VPN_CONNECT_TIMEOUT` | `15` | Tunnel establishment timeout (seconds); not applied to video downloads |
| `VPN_VERIFY_TIMEOUT` | `10` | Exit-IP lookup timeout for the Test button |

### Local checks

Unit tests (no live VPN):

```bash
python -m unittest tests.test_vpn
```

Real tunnel (needs `/dev/net/tun` and `NET_ADMIN`):

```bash
docker build -t vkget-vpn-gateway:local vpn-gateway
docker run --rm --cap-add=NET_ADMIN --device /dev/net/tun \
  -p 8081:8081 -p 1080:1080 vkget-vpn-gateway:local
# POST {"type":"openvpn"|"wireguard","config":"..."} to http://127.0.0.1:8081/connect
# then: curl --proxy socks5://127.0.0.1:1080 https://ifconfig.co/json
```

## System check

The `/system` page inspects components inside the vkget container:

- image build SHA (`VKGET_GIT_SHA`)
- yt-dlp (installed version vs the latest GitHub release)
- ffmpeg / ffprobe
- cookies file
- download storage
- database
- Python runtime

It does not upgrade binaries in the running pod.

**YT-DLP status**

- `OK` — the installed (or image) version matches GitHub, or GitHub could not be reached
- `UPDATE` — GitHub has a newer release than this image
- `TIMEOUT` / `ERROR` — the live `yt-dlp --version` probe failed. That is not an update. On a small ARM node the zipapp can exceed the probe window; vkget then uses `YTDLP_VERSION` baked into the image.

To update yt-dlp:

1. Set `YTDLP_VERSION` in the Dockerfile to the GitHub release tag (currently `2026.08.19`).
2. Push to `main` so Actions publishes `:latest` and `:<short-sha>`.
3. Set both `newTag` fields in `k8s/kustomization.yaml` to that short SHA and apply / let Flux reconcile.

`/healthz` stays a cheap liveness probe and does not call GitHub.

## Deploy

Each push to `main` publishes `linux/arm64` images as:

- `ghcr.io/bogdanovj/vkget:latest` and `ghcr.io/bogdanovj/vkget-vpn-gateway:latest` (moving pointers)
- `ghcr.io/bogdanovj/vkget:<short-sha>` and `ghcr.io/bogdanovj/vkget-vpn-gateway:<short-sha>` (immutable)

`:latest` is only a convenience tag. The cluster pin is `images.newTag` in `k8s/kustomization.yaml`.

1. Push to `main` and wait for the Publish workflow.
2. When you want that build in-cluster, set both `newTag` values to the short SHA from the Actions log or GHCR package, then apply / let Flux reconcile. A pin change is the rollout; `kubectl rollout restart` is not how you pick up a new `:latest`.
3. Confirm `/system` shows the same IMAGE SHA as the pin.
4. Create the MariaDB database/user with `sql/bootstrap.sql`.
5. Create SOPS secrets from the examples in `k8s/`.
6. Set the storage node selector in `k8s/deployment.yaml`.
7. Apply with Flux/Kustomize.
