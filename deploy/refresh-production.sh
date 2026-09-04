#!/usr/bin/env bash
set -Eeuo pipefail

current_link="/opt/sigma-tape/current"
state_cache="/var/lib/sigma-tape/dashboard-state.json"
cache_name="dashboard-state.json"
services=()
case "${1:-all}" in
  all) ;;
  yesterday)
    cache_name="yesterday-state.json"
    state_cache="/var/lib/sigma-tape/$cache_name"
    services=(backend)
    ;;
  *) echo "Unknown refresh scope" >&2; exit 2 ;;
esac
# The two jobs share intermediate downloads, but never their published cache.
exec 9>/var/lib/sigma-tape/refresh.lock
flock -x 9
release="$(readlink -f "$current_link")"

if [[ -z "$release" || ! -d "$release" ]]; then
  echo "Option Sniper current release is unavailable" >&2
  exit 1
fi

revision="$(basename "$release")"
before="missing"
if [[ -f "$state_cache" ]]; then
  before="$(sha256sum "$state_cache" | awk '{print $1}')"
fi

cd "$release"
env SIGMA_TAPE_VERSION="$revision" \
  docker compose -f deploy/compose.yml run --rm --no-deps \
  --entrypoint python backend -m scripts.refresh_dashboard_cache \
  --state-cache "/app/.cache/sigma-tape/$cache_name"

after="$(sha256sum "$state_cache" | awk '{print $1}')"
if [[ "$before" == "$after" ]]; then
  echo "Dashboard cache already covers the latest completed session"
  exit 0
fi

env SIGMA_TAPE_VERSION="$revision" \
  docker compose -f deploy/compose.yml up -d --no-build --force-recreate "${services[@]}"

for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8765/api/health >/dev/null 2>&1; then
    echo "Dashboard cache refreshed and production adapter restarted"
    exit 0
  fi
  sleep 5
done

echo "Production adapter did not become healthy after the cache refresh" >&2
docker logs --tail 100 sigma-tape >&2 || true
exit 1
