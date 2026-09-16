#!/usr/bin/env bash
set -Eeuo pipefail

revision="${1:?revision is required}"
expected_sha256="${2:?archive checksum is required}"

if [[ ! "$revision" =~ ^[0-9a-f]{8,40}$ ]]; then
  echo "Invalid revision" >&2
  exit 2
fi
if [[ ! "$expected_sha256" =~ ^[0-9a-f]{64}$ ]]; then
  echo "Invalid archive checksum" >&2
  exit 2
fi

archive="/tmp/option-sniper-staging-${revision}.tar.gz"
release_root="/opt/sigma-tape-staging/releases"
release="$release_root/$revision"
current_link="/opt/sigma-tape-staging/current"
previous="$(readlink -f "$current_link" 2>/dev/null || true)"
production_environment="/etc/sigma-tape/sigma-tape.env"
staging_environment="/etc/sigma-tape/sigma-tape-staging.env"
production_cache="/var/lib/sigma-tape"
staging_cache="/var/lib/sigma-tape-staging"
release_staging=""
environment_temp=""

cleanup() {
  rm -f "$archive"
  if [[ -n "$release_staging" && -d "$release_staging" ]]; then
    sudo rm -rf "$release_staging"
  fi
  if [[ -n "$environment_temp" ]]; then
    sudo rm -f "$environment_temp"
  fi
}
trap cleanup EXIT

actual_sha256="$(sha256sum "$archive" | awk '{print $1}')"
if [[ "$actual_sha256" != "$expected_sha256" ]]; then
  echo "Staging release archive checksum mismatch" >&2
  exit 1
fi

sudo test -r "$production_environment"
sudo test -d "$production_cache"
sudo install -d -m 0755 "$release_root"
sudo install -d -o 10001 -g 10001 -m 0750 "$staging_cache"

# Staging receives market-data credentials but never the production Discord token.
environment_temp="$(sudo mktemp /etc/sigma-tape/.sigma-tape-staging.env.XXXXXX)"
sudo grep -E \
  '^(DATABENTO_(API_KEY|LIVE_API_KEY|SPOT_API_KEY|LIVE_OPTION_API_KEY|LIVE_OPTIONS_API_KEY|HISTORICAL_OPTION_API_KEY|HISTORICAL_OPTIONS_API_KEY|OPTION_API_KEY|OPTIONS_API_KEY)|SIGMA_(S3_BUCKET|S3_REGION|S3_PREFIX|RECONSTRUCTION_MAX_COST))=' \
  "$production_environment" | sudo tee "$environment_temp" >/dev/null || true
# Preserve the separately configured staging live-options credential on redeploy.
if sudo test -f "$staging_environment" && \
   sudo grep -q '^DATABENTO_LIVE_OPTIONS_API_KEY=.' "$staging_environment"; then
  sudo sed -i '/^DATABENTO_LIVE_OPTIONS_API_KEY=/d' "$environment_temp"
  sudo grep '^DATABENTO_LIVE_OPTIONS_API_KEY=.' "$staging_environment" \
    | sudo tee -a "$environment_temp" >/dev/null
fi
printf '%s\n' \
  'SIGMA_ALLOWED_ORIGINS=https://staging.optionsniper.xyz' \
  | sudo tee -a "$environment_temp" >/dev/null
sudo chown root:root "$environment_temp"
sudo chmod 0600 "$environment_temp"
sudo mv "$environment_temp" "$staging_environment"
environment_temp=""

# Seed only immutable/read-model caches on first deployment. Alert state is excluded.
for cache_name in \
  dashboard-state.json \
  yesterday-state.json \
  live-prices.json \
  live-iv-expansion.json \
  option-contracts.json; do
  if ! sudo test -e "$staging_cache/$cache_name" && \
     sudo test -f "$production_cache/$cache_name"; then
    sudo install -o 10001 -g 10001 -m 0640 \
      "$production_cache/$cache_name" "$staging_cache/$cache_name"
  fi
done

if [[ ! -f "$release/.release-revision" ]] || \
   [[ "$(cat "$release/.release-revision" 2>/dev/null)" != "$revision" ]]; then
  if [[ -e "$release" ]]; then
    echo "Staging release path exists without a matching revision marker: $release" >&2
    exit 1
  fi
  release_staging="$(sudo mktemp -d "$release_root/.${revision}.XXXXXX")"
  sudo tar -xzf "$archive" -C "$release_staging"
  printf '%s\n' "$revision" | sudo tee "$release_staging/.release-revision" >/dev/null
  sudo chown -R root:root "$release_staging"
  sudo mv "$release_staging" "$release"
  release_staging=""
fi
sudo chmod 0755 "$release"

cd "$release"
sudo env SIGMA_TAPE_VERSION="$revision" \
  docker compose -f deploy/compose.staging.yml build

# Refresh the OCC list, falling back to a validated cached list on request failure before restarting.
# Failure leaves the currently serving release untouched.
sudo env SIGMA_TAPE_VERSION="$revision" \
  docker compose -f deploy/compose.staging.yml run --rm --no-deps \
  --entrypoint python backend -m scripts.weekly_options

# Semantic readiness and code/cache rollback are shared with production.
sudo bash "$release/deploy/guarded-activate.sh" staging "$revision"
curl -fsS --max-time 10 http://127.0.0.1:8766/api/health >/dev/null
sudo docker inspect --format \
  'staging image={{.Config.Image}} status={{.State.Status}} health={{.State.Health.Status}} restarts={{.RestartCount}}' \
  sigma-tape-staging
