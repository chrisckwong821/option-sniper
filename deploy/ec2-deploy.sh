#!/usr/bin/env bash
set -Eeuo pipefail

revision="${1:?revision is required}"
expected_sha256="${2:?archive checksum is required}"
discord_token_file="${3:?Discord token file is required}"
skip_cache_guard="${4:-0}"

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
if [[ "$skip_cache_guard" != 0 && "$skip_cache_guard" != 1 ]]; then
  echo "Invalid cache guard setting" >&2
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

cd "$release"
sudo env SIGMA_TAPE_VERSION="$revision" \
  docker compose -f deploy/compose.yml build

# Refresh the OCC list, falling back to a validated cached list on request failure before restarting.
# Failure leaves the currently serving release untouched.
sudo env SIGMA_TAPE_VERSION="$revision" \
  docker compose -f deploy/compose.yml run --rm --no-deps \
  --entrypoint python backend -m scripts.weekly_options

# Fail-closed semantic checks, including the alerter, and code/cache rollback.
sudo env SIGMA_SKIP_CACHE_GUARD="$skip_cache_guard" \
  bash "$release/deploy/guarded-activate.sh" production "$revision"
sudo docker inspect --format \
  'deployed image={{.Config.Image}} status={{.State.Status}} health={{.State.Health.Status}} restarts={{.RestartCount}}' \
  sigma-tape
sudo docker inspect --format \
  'alerter image={{.Config.Image}} status={{.State.Status}} health={{.State.Health.Status}} restarts={{.RestartCount}}' \
  sigma-tape-alerter
