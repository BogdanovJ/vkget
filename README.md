# VKGET

Self-hosted VK/VKVideo subscription manager for k3s.

Features:
- one-off URLs
- playlist/channel subscriptions
- initial "last N" import
- future discovery with remembered history
- duration and stop-word filtering
- hard 720p ceiling
- one active download at a time
- randomized discovery/retry scheduling
- MariaDB persistence
- Telegram notifications
- channel/uploader folder sorting
- `/mnt/downloads` host mount exposed as `/downloads`
- minimal 16-bit-inspired UI

## Storage

Kubernetes mounts the node's existing `/mnt/downloads` to `/downloads`.

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

## Deploy

1. Push to `main`. GitHub Actions builds `linux/arm64` and publishes `ghcr.io/bogdanovj/vkget:latest`.
2. Restart the workload yourself so it pulls the new image, e.g. `kubectl -n vkget rollout restart deploy/vkget`.
3. Create the MariaDB database/user with `sql/bootstrap.sql`.
4. Create SOPS secrets from the examples in `k8s/`.
5. Set the storage node selector in `k8s/deployment.yaml`.
6. Change the image reference.
7. Apply with Flux/Kustomize.
