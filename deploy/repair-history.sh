#!/usr/bin/env bash
set -Eeuo pipefail
environment="${1:-production}"
case "$environment" in
  production) base=/opt/sigma-tape; cache=/var/lib/sigma-tape; image=sigma-tape; envfile=/etc/sigma-tape/sigma-tape.env ;;
  staging) base=/opt/sigma-tape-staging; cache=/var/lib/sigma-tape-staging; image=sigma-tape-staging; envfile=/etc/sigma-tape/sigma-tape-staging.env ;;
  *) exit 2 ;;
esac
# Serialize with deployments and scheduled cache jobs, without delaying serving.
exec 9>"$cache/refresh.lock"
flock -n 9 || exit 0
release="$(readlink -f "$base/current")"
revision="$(basename "$release")"
[[ "$revision" =~ ^[a-z0-9][a-z0-9-]{6,60}$ ]] || exit 2
docker run --rm --init --name "$image-history-repair" \
  --memory=1280m --memory-swap=1280m --cpus=0.5 --pids-limit=128 \
  --read-only --tmpfs /tmp:size=64m --cap-drop=ALL --security-opt=no-new-privileges:true \
  --env-file "$envfile" -v "$cache:/app/.cache/sigma-tape" \
  --entrypoint python "$image:$revision" -m scripts.history_repair_worker
