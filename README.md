# VKGET

Self-hosted VK/VKVideo subscription manager for k3s.

Features:
- one-off URLs
- playlist/channel subscriptions
- initial "last N" import
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

## Russian VPN fallback

VK / VK Video downloads can fail or throttle when the cluster egress is outside Russia. vkget can retry those downloads through a public Russian OpenVPN relay **without** sending the web UI, MariaDB, scheduler, Telegram, or Kubernetes traffic through the VPN.

Only yt-dlp download attempts use a SOCKS proxy on a dedicated `vkget-vpn-gateway` Deployment. That gateway is the only container with `NET_ADMIN` and `/dev/net/tun`. The vkget app container stays unprivileged.

Endpoint catalogue:

- Primary machine-readable source: the [VPN Gate CSV API](https://www.vpngate.net/api/iphone/) (`CountryShort == RU`, usable OpenVPN configs only).
- Extra Russia-specific discovery: [VPN Obratno](https://vpnobratno.info/en/). HTML/parser failures must not break vkget.
- Rows are keyed by public IPv4. The same server from both sources is one row.
- OpenVPN profiles, health, and scores are stored in MariaDB (`vpn_endpoints`). Missing servers age out (`VPN_STALE_AFTER_HOURS`, then `VPN_DISABLE_AFTER_DAYS`) instead of being deleted on one refresh.

`VK_VPN_MODE`:

- `off` — never use the VPN
- `auto` (default) — download directly first; use a Russian VPN only for geo/throttle/network-style failures
- `always` — try a verified Russian VPN first; if the gateway or catalogue is down, fall back to a direct download so the queue does not stall

New environment variables (also in `.env.example` and `k8s/configmap.yaml`):

| Variable | Default | Meaning |
|---|---|---|
| `VK_VPN_MODE` | `auto` | `off`, `auto`, or `always` |
| `VPN_DISCOVERY_ENABLED` | `true` | Refresh the RU catalogue from the in-process scheduler |
| `VPN_DISCOVERY_INTERVAL_MINUTES` | `30` | Catalogue refresh interval |
| `VPN_GATEWAY_URL` | in-cluster gateway `:8081` | Control API (`/connect`, `/disconnect`, `/status`) |
| `VPN_PROXY_URL` | in-cluster SOCKS `:1080` | Passed to yt-dlp as `--proxy` |
| `VPN_MAX_ENDPOINT_ATTEMPTS` | `3` | Endpoints to try per download |
| `VPN_CONNECT_TIMEOUT` | `20` | OpenVPN connect timeout (seconds) |
| `VPN_VERIFY_TIMEOUT` | `10` | RU exit-IP lookup timeout |
| `VPN_MIN_DOWNLOAD_RATE` | `300K` | Rotate after a sustained slow VPN transfer |
| `VPN_SLOW_RATE_DURATION` | `120` | Seconds below the floor before rotate |
| `VPN_STALE_AFTER_HOURS` | `24` | Mark unseen servers stale |
| `VPN_DISABLE_AFTER_DAYS` | `7` | Mark unseen servers inactive |
| `VPN_GATE_CSV_URL` | VPN Gate API | Override the CSV URL |
| `VPN_OBRATNO_URL` | VPN Obratno EN page | Override the HTML URL |

UI: Home shows a Russian VPN status panel. `/vpn` lists endpoints with Test / Enable / Disable / Refresh. Diagnostics: `GET /api/vpn/status`, `GET /api/vpn/endpoints`, `POST /api/vpn/refresh`, `POST /api/vpn/test/{id}`. There is no extra auth layer; the existing LAN Ingress trust boundary applies. The gateway Service has no Ingress.

### Local checks

Unit tests (no live VPN):

```bash
python -m unittest tests.test_vpn
```

Catalogue only, against a running app/DB:

```bash
# VPN_DISCOVERY_ENABLED=true
curl -X POST http://vkget.lan/api/vpn/refresh
curl http://vkget.lan/api/vpn/endpoints
```

Real OpenVPN (needs `/dev/net/tun` and `NET_ADMIN`):

```bash
docker build -t vkget-vpn-gateway:local vpn-gateway
docker run --rm --cap-add=NET_ADMIN --device /dev/net/tun \
  -p 8081:8081 -p 1080:1080 vkget-vpn-gateway:local
# POST a sanitized .ovpn to http://127.0.0.1:8081/connect
# then: curl --proxy socks5://127.0.0.1:1080 https://ifconfig.co/json
```

One VK download through Russia:

```bash
# VK_VPN_MODE=always
# queue a video in the UI, then confirm logs:
#   vkget: VPN endpoint selected: …
#   vkget: VPN external IP verified: country=RU
#   yt-dlp --proxy socks5://vkget-vpn-gateway:1080
```

## Deploy

1. Push to `main`. GitHub Actions builds `linux/arm64` and publishes `ghcr.io/bogdanovj/vkget:latest`.
2. Restart the workload yourself so it pulls the new image, e.g. `kubectl -n vkget rollout restart deploy/vkget`.
3. Create the MariaDB database/user with `sql/bootstrap.sql`.
4. Create SOPS secrets from the examples in `k8s/`.
5. Set the storage node selector in `k8s/deployment.yaml`.
6. Change the image reference.
7. Apply with Flux/Kustomize.
