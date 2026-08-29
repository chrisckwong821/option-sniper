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

archive="/tmp/option-sniper-${revision}.tar.gz"
release_root="/opt/sigma-tape/releases"
release="$release_root/$revision"
current_link="/opt/sigma-tape/current"
previous="$(readlink -f "$current_link" 2>/dev/null || true)"
staging=""

cleanup() {
  rm -f "$archive"
  if [[ -n "$staging" && -d "$staging" ]]; then
    sudo rm -rf "$staging"
  fi
}
trap cleanup EXIT

actual_sha256="$(sha256sum "$archive" | awk '{print $1}')"
if [[ "$actual_sha256" != "$expected_sha256" ]]; then
  echo "Release archive checksum mismatch" >&2
  exit 1
fi

sudo test -r /etc/sigma-tape/sigma-tape.env
sudo test -d /var/lib/sigma-tape
sudo install -d -m 0755 "$release_root"

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
  docker compose -f deploy/compose.yml up -d --no-build; then
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
sudo docker inspect --format \
  'deployed image={{.Config.Image}} status={{.State.Status}} health={{.State.Health.Status}} restarts={{.RestartCount}}' \
  sigma-tape

