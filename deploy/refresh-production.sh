#!/usr/bin/env bash
set -Eeuo pipefail

current_link="/opt/sigma-tape/current"
state_cache="/var/lib/sigma-tape/dashboard-state.json"
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
  --entrypoint python backend -m scripts.refresh_dashboard_cache

after="$(sha256sum "$state_cache" | awk '{print $1}')"
if [[ "$before" == "$after" ]]; then
  echo "Dashboard cache already covers the latest completed session"
  exit 0
fi

env SIGMA_TAPE_VERSION="$revision" \
  docker compose -f deploy/compose.yml up -d --no-build --force-recreate

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
