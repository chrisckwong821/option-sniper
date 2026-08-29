# EC2 runtime

Option Sniper runs as a non-root Docker container and binds only to the EC2
loopback interface. A reverse proxy or Cloudflare Tunnel should be the only
public path to the service.

Host-owned runtime paths:

- `/opt/sigma-tape`: release source and Compose definition
- `/etc/sigma-tape/sigma-tape.env`: Databento credentials, root-owned `0600`
- `/var/lib/sigma-tape`: private dashboard and live-price cache, UID `10001`

The EC2 host uses immutable releases under `/opt/sigma-tape/releases/<revision>`
and points `/opt/sigma-tape/current` at the active release. Build and start a
release with the same revision as its image tag:

```bash
revision=YOUR_COMMIT_ID
cd "/opt/sigma-tape/releases/$revision"
sudo env SIGMA_TAPE_VERSION="$revision" docker compose -f deploy/compose.yml build
sudo env SIGMA_TAPE_VERSION="$revision" docker compose -f deploy/compose.yml up -d
sudo ln -sfn "/opt/sigma-tape/releases/$revision" /opt/sigma-tape/current
sudo env SIGMA_TAPE_VERSION="$revision" docker compose -f deploy/compose.yml ps
curl --fail http://127.0.0.1:8765/api/health
```

To roll back, run `docker compose up -d --no-build` from a retained release
with `SIGMA_TAPE_VERSION` set to that release's image tag, then repoint the
`current` link.

Create the credential file once, outside the release tree:

```bash
sudo install -d -o root -g root -m 0700 /etc/sigma-tape
sudo install -o root -g root -m 0600 /dev/null /etc/sigma-tape/sigma-tape.env
sudoedit /etc/sigma-tape/sigma-tape.env
```

It must define `DATABENTO_LIVE_API_KEY` and `DATABENTO_OPTIONS_API_KEY`.
Do not add either value to an image, release archive, or GitHub Actions log.

The container uses `restart: unless-stopped`, so Docker restores it after a host
reboot. The image contains no credentials or market snapshots. A mounted cache
is accepted only when it matches the latest completed Nasdaq session; otherwise
the backend performs its bounded historical refresh before opening the server.

For public access, keep port `8765` bound to loopback and publish
`http://127.0.0.1:8765` through a Cloudflare Tunnel. Do not cache `/api/*` or
`/ws/*`; only static assets are suitable for edge caching.

## Cloudflare Tunnel

Install `cloudflared` from Cloudflare's signed package repository, then create a
remotely managed tunnel in the Cloudflare dashboard. On the tunnel's setup page,
copy only its Linux service-install command and run it on the EC2 host:

```bash
sudo cloudflared service install YOUR_TUNNEL_TOKEN
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared --no-pager
```

Treat the tunnel token as a secret. Do not put it in this repository, the
application environment file, or a shell script.

Add a published application route with the intended public hostname and this
origin service URL:

```text
http://127.0.0.1:8765
```

After the hostname works, restrict browser WebSocket origins by adding its exact
HTTPS origin to `/etc/sigma-tape/sigma-tape.env`:

```bash
SIGMA_ALLOWED_ORIGINS=https://tape.example.com
```

Use `sudoedit /etc/sigma-tape/sigma-tape.env` to add the setting without changing
the file's root ownership, then recreate the backend:

```bash
cd /opt/sigma-tape/current
revision=$(basename "$(readlink -f /opt/sigma-tape/current)")
sudo env SIGMA_TAPE_VERSION="$revision" docker compose -f deploy/compose.yml up -d
```

Verify the public dashboard, API, and WebSocket before tightening any remaining
AWS security-group rules. Port `8765` must remain closed to public ingress.

## Automated deployment

`.github/workflows/deploy.yml` runs only after the `CI` workflow succeeds for a
push to `main`. It deploys the exact tested commit with `deploy/ec2-deploy.sh`.
The script validates the archive checksum, preserves immutable releases, waits
up to 15 minutes for startup, and restores the prior release if health checks
fail.

The GitHub `production` environment must define these encrypted secrets:

- `EC2_HOST`: EC2 hostname or IP address.
- `EC2_USER`: SSH login user.
- `EC2_SSH_KEY`: private deployment key.
- `EC2_KNOWN_HOSTS`: pinned OpenSSH host-key line for `EC2_HOST`.

Historical and live Databento keys remain in
`/etc/sigma-tape/sigma-tape.env`; they are not GitHub deployment secrets.
