#!/usr/bin/env bash
# Run as root after building the candidate image. No credential changes here.
set -Eeuo pipefail
environment="${1:?environment required}"
revision="${2:?revision required}"
[[ "$revision" =~ ^[a-z0-9][a-z0-9-]{6,60}$ ]] || exit 2
case "$environment" in
  staging)
    base=/opt/sigma-tape-staging
    cache=/var/lib/sigma-tape-staging
    backend=sigma-tape-staging
    compose=deploy/compose.staging.yml
    services=(backend)
    ;;
  production)
    base=/opt/sigma-tape
    cache=/var/lib/sigma-tape
    backend=sigma-tape
    compose=deploy/compose.yml
    services=(backend alerter)
    ;;
  *) exit 2 ;;
esac
release="$base/releases/$revision"
previous="$(readlink -f "$base/current")"
[[ "$previous" == "$base/releases/"* && -d "$previous" && -d "$release" ]] || exit 2
previous_revision="$(basename "$previous")"
[[ "$previous_revision" =~ ^[a-z0-9][a-z0-9-]{6,60}$ ]] || exit 2
# Share the daily-refresh lock; never snapshot caches while refresh writes them.
exec 9>"$cache/refresh.lock"
flock -x 9
install -d -m 0700 "$cache-deploy-backups"
snapshot="$(mktemp -d "$cache-deploy-backups/$revision.XXXXXX")"
stopped=false
saved=false
timers_saved=false
timer_names=(option-sniper-refresh option-sniper-yesterday-refresh option-sniper-reopening-refresh option-sniper-history-repair)
save_timers() {
  [[ "$environment" == production ]] || return 0
  for name in "${timer_names[@]}"; do
    for extension in service timer; do
      unit="$name.$extension"
      if [[ -f "/etc/systemd/system/$unit" ]]; then
        cp -p "/etc/systemd/system/$unit" "$snapshot/$unit"
      fi
    done
    if systemctl is-enabled --quiet "$name.timer"; then touch "$snapshot/$name.enabled"; fi
    if systemctl is-active --quiet "$name.timer"; then touch "$snapshot/$name.active"; fi
  done
  timers_saved=true
}
restore_timers() {
  [[ "$timers_saved" == true ]] || return 0
  for name in "${timer_names[@]}"; do
    systemctl stop "$name.timer" || true
    systemctl disable "$name.timer" || true
    for extension in service timer; do
      unit="$name.$extension"
      if [[ -f "$snapshot/$unit" ]]; then
        cp -p "$snapshot/$unit" "/etc/systemd/system/$unit" || return 1
      else
        rm -f "/etc/systemd/system/$unit" || return 1
      fi
    done
  done
  systemctl daemon-reload || return 1
  for name in "${timer_names[@]}"; do
    if [[ -f "$snapshot/$name.enabled" ]]; then systemctl enable "$name.timer" || return 1; fi
    if [[ -f "$snapshot/$name.active" ]]; then systemctl start "$name.timer" || return 1; fi
  done
}
compose_at() {
  local directory="$1" version="$2"
  shift 2
  (trap - ERR; cd "$directory"; env SIGMA_TAPE_VERSION="$version" docker compose -f "$compose" "$@")
}
rollback() {
  local code="${1:-1}"
  trap - ERR INT TERM
  set +e
  echo "Activation failed; restoring $previous_revision with cache snapshot $snapshot" >&2
  if [[ "$stopped" == true ]]; then
    if ! compose_at "$release" "$revision" stop -t 3 "${services[@]}"; then
      echo 'Could not stop candidate safely; refusing live cache replacement' >&2
      exit 1
    fi
    if [[ "$saved" == true ]]; then
      if ! python3 "$release/scripts/deployment_cache.py" restore "$cache" "$snapshot"; then
        echo 'Cache restore failed; services left stopped to prevent incompatible startup' >&2
        exit 1
      fi
    fi
    if ! compose_at "$previous" "$previous_revision" up -d --no-build --force-recreate "${services[@]}"; then
      echo 'Previous release restart failed; operator action required' >&2
      exit 1
    fi
    ln -sfn "$previous" "$base/current"
    if ! restore_timers; then
      echo 'Previous code/cache restored, but timer restoration needs operator attention' >&2
      exit 1
    fi
    recovered=false
    for _ in $(seq 1 60); do
      state="$(docker inspect --format '{{.State.Status}} {{.State.Health.Status}}' "$backend")"
      alert_ok=true
      if [[ "$environment" == production ]]; then
        state_alert="$(docker inspect --format '{{.State.Status}} {{.State.Health.Status}}' sigma-tape-alerter)"
        [[ "$state_alert" == 'running healthy' ]] || alert_ok=false
      fi
      if [[ "$state" == 'running healthy' && "$alert_ok" == true ]]; then recovered=true; break; fi
      sleep 1
    done
    if [[ "$recovered" != true ]]; then
      echo 'Rollback containers did not recover health; operator action required' >&2
      exit 1
    fi
    echo 'Rollback restored previous code and version-dependent cache; alert history retained' >&2
  fi
  exit "$code"
}
trap 'rollback $?' ERR
trap 'rollback 130' INT TERM
if [[ "$environment" == production ]]; then
  # Capture before touching production; a missing staging reference fails safely.
  python3 "$release/scripts/verify_sigma_parity.py" --capture \
    --reference "$snapshot-staging-sigma.json" --url http://127.0.0.1:8766/api/state
fi
stopped=true
compose_at "$previous" "$previous_revision" stop "${services[@]}"
python3 "$release/scripts/deployment_cache.py" backup "$cache" "$snapshot"
saved=true
save_timers
if [[ "$environment" == production ]]; then
  compose_at "$release" "$revision" up -d --no-build --force-recreate backend
  # Check before starting the alerter or accepting the new release. A data
  # mismatch immediately takes the cache/image rollback path, without a build.
  python3 "$release/scripts/verify_sigma_parity.py" \
    --reference "$snapshot-staging-sigma.json" --url http://127.0.0.1:8765/api/state --wait-seconds 30
  compose_at "$release" "$revision" up -d --no-build alerter
else
  compose_at "$release" "$revision" up -d --no-build --force-recreate "${services[@]}"
fi
# Explicit failure injection is restricted to staging for rollback drills.
if [[ "$environment" == staging && "${SIGMA_STAGING_FAIL_HEALTH:-0}" == 1 ]]; then
  echo 'Intentional staging readiness failure' >&2
  false
fi
ready=false
for _ in $(seq 1 30); do
  backend_state="$(docker inspect --format '{{.State.Status}} {{.State.Health.Status}}' "$backend")"
  alerter_ok=true
  if [[ "$environment" == production ]]; then
    alerter_state="$(docker inspect --format '{{.State.Status}} {{.State.Health.Status}}' sigma-tape-alerter)"
    [[ "$alerter_state" == 'running healthy' ]] || alerter_ok=false
  fi
  if [[ "$backend_state" == 'running healthy' && "$alerter_ok" == true ]] && \
     docker exec "$backend" python -m scripts.deployment_health && \
     docker exec "$backend" python -c 'import urllib.request; assert "<title>Option Sniper</title>" in urllib.request.urlopen("http://127.0.0.1:8765/", timeout=10).read().decode(); assert "createLoader" in urllib.request.urlopen("http://127.0.0.1:8765/state-refresh.js", timeout=10).read().decode()'; then
    ready=true
    break
  fi
  sleep 2
done
if [[ "$ready" != true ]]; then
  echo 'Release readiness failed (including Discord alerter in production)' >&2
  false
fi
ln -sfn "$release" "$base/current"
if [[ "$environment" == production ]]; then
  for name in "${timer_names[@]}"; do
    for extension in service timer; do
      install -o root -g root -m 0644 "$release/deploy/$name.$extension" "/etc/systemd/system/$name.$extension"
    done
  done
  systemctl daemon-reload
  for name in "${timer_names[@]}"; do
    systemctl enable "$name.timer"
    systemctl restart "$name.timer"
  done
  systemctl list-timers 'option-sniper*refresh.timer' --no-pager
fi
trap - ERR INT TERM
echo "Activation verified: $revision; rollback snapshot retained at $snapshot"
