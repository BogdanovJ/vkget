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

## Deploy

1. Build/push the image.
2. Create the MariaDB database/user with `sql/bootstrap.sql`.
3. Create SOPS secrets from the examples in `k8s/`.
4. Set the storage node selector in `k8s/deployment.yaml`.
5. Change the image reference.
6. Apply with Flux/Kustomize.
