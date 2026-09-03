#!/usr/bin/env bash
set -Eeuo pipefail

revision="${1:?revision is required}"
expected_sha256="${2:?archive checksum is required}"
discord_token_file="${3:?Discord token file is required}"

if [[ ! "$revision" =~ ^[0-9a-f]{8,40}$ ]]; then
  echo "Invalid revision" >&2
  exit 2
fi
if [[ ! "$expected_sha256" =~ ^[0-9a-f]{64}$ ]]; then
  echo "Invalid archive checksum" >&2
  exit 2
fi
if [[ "$discord_token_file" != "/tmp/option-sniper-discord-token-$revision" ]]; then
  echo "Invalid Discord token path" >&2
  exit 2
fi

archive="/tmp/option-sniper-${revision}.tar.gz"
release_root="/opt/sigma-tape/releases"
release="$release_root/$revision"
current_link="/opt/sigma-tape/current"
previous="$(readlink -f "$current_link" 2>/dev/null || true)"
staging=""
environment_temp=""

cleanup() {
  rm -f "$archive"
  rm -f "$discord_token_file"
  if [[ -n "$staging" && -d "$staging" ]]; then
    sudo rm -rf "$staging"
  fi
  if [[ -n "$environment_temp" ]]; then
    sudo rm -f "$environment_temp"
  fi
}
trap cleanup EXIT

actual_sha256="$(sha256sum "$archive" | awk '{print $1}')"
if [[ "$actual_sha256" != "$expected_sha256" ]]; then
  echo "Release archive checksum mismatch" >&2
  exit 1
fi

if [[ ! -s "$discord_token_file" ]] || \
   [[ "$(wc -l < "$discord_token_file")" -gt 0 ]]; then
  echo "Discord token file is empty or contains a newline" >&2
  exit 1
fi

environment_file="/etc/sigma-tape/sigma-tape.env"
sudo test -r "$environment_file"
sudo test -d /var/lib/sigma-tape
sudo install -d -m 0755 "$release_root"

environment_temp="$(sudo mktemp /etc/sigma-tape/.sigma-tape.env.XXXXXX)"
sudo grep -v '^DISCORD_BOT_TOKEN=' "$environment_file" \
  | sudo tee "$environment_temp" >/dev/null || true
printf 'DISCORD_BOT_TOKEN=' | sudo tee -a "$environment_temp" >/dev/null
sudo tee -a "$environment_temp" < "$discord_token_file" >/dev/null
printf '\n' | sudo tee -a "$environment_temp" >/dev/null
sudo chown root:root "$environment_temp"
sudo chmod 0600 "$environment_temp"
sudo mv "$environment_temp" "$environment_file"
environment_temp=""

if [[ ! -f "$release/.release-revision" ]] || \
   [[ "$(cat "$release/.release-revision" 2>/dev/null)" != "$revision" ]]; then
  if [[ -e "$release" ]]; then
    echo "Release path exists without a matching revision marker: $release" >&2
    exit 1
  fi
  staging="$(sudo mktemp -d "$release_root/.${revision}.XXXXXX")"
  sudo tar -xzf "$archive" -C "$staging"
  printf '%s\n' "$revision" | sudo tee "$staging/.release-revision" >/dev/null
  sudo chown -R root:root "$staging"
  sudo mv "$staging" "$release"
  staging=""
fi
sudo chmod 0755 "$release"

rollback() {
  if [[ -z "$previous" || ! -d "$previous" || "$previous" == "$release" ]]; then
    echo "No prior release is available for rollback" >&2
    return
  fi
  previous_revision="$(basename "$previous")"
  echo "Rolling back to $previous_revision" >&2
  (
    cd "$previous"
    sudo env SIGMA_TAPE_VERSION="$previous_revision" \
      docker compose -f deploy/compose.yml up -d --no-build
  )
  sudo ln -sfn "$previous" "$current_link"
}

cd "$release"
sudo env SIGMA_TAPE_VERSION="$revision" \
  docker compose -f deploy/compose.yml build

if ! sudo env SIGMA_TAPE_VERSION="$revision" \
  docker compose -f deploy/compose.yml up -d --no-build --force-recreate; then
  rollback
  exit 1
fi

healthy=false
for _ in $(seq 1 180); do
  if curl -fsS http://127.0.0.1:8765/api/health >/dev/null 2>&1 && \
     curl -fsS http://127.0.0.1:8765/ | grep -q '<title>Option Sniper</title>'; then
    healthy=true
    break
  fi
  sleep 5
done

if [[ "$healthy" != true ]]; then
  echo "Option Sniper failed its production health check" >&2
  sudo docker logs --tail 100 sigma-tape 2>&1 || true
  rollback
  exit 1
fi

sudo ln -sfn "$release" "$current_link"
sudo install -o root -g root -m 0644 \
  "$release/deploy/option-sniper-refresh.service" \
  /etc/systemd/system/option-sniper-refresh.service
sudo install -o root -g root -m 0644 \
  "$release/deploy/option-sniper-refresh.timer" \
  /etc/systemd/system/option-sniper-refresh.timer
sudo systemctl daemon-reload
sudo systemctl enable --now option-sniper-refresh.timer

for _ in $(seq 1 12); do
  alerter_state="$(sudo docker inspect --format \
    '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' \
    sigma-tape-alerter 2>/dev/null || true)"
  if [[ "$alerter_state" == "running healthy" ]] || \
     [[ "$alerter_state" != running* ]]; then
    break
  fi
  sleep 5
done

sudo docker inspect --format \
  'deployed image={{.Config.Image}} status={{.State.Status}} health={{.State.Health.Status}} restarts={{.RestartCount}}' \
  sigma-tape
sudo docker inspect --format \
  'alerter image={{.Config.Image}} status={{.State.Status}} health={{.State.Health.Status}} restarts={{.RestartCount}}' \
  sigma-tape-alerter
if [[ "$alerter_state" != "running healthy" ]]; then
  echo "Discord alerter did not become healthy; safe diagnostic output follows" >&2
  sudo docker logs --tail 20 sigma-tape-alerter >&2 || true
else
  sleep 30
  sudo docker logs --since 5m sigma-tape-alerter 2>&1 \
    | grep -E '"event": "(alert_stream_connecting|discord_alert_sent|discord_alert_uncertain|discord_alert_released)"' \
    | tail -n 100 || true
fi
