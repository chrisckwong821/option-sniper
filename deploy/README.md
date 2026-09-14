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

It must define `DATABENTO_LIVE_API_KEY`, `DATABENTO_LIVE_OPTIONS_API_KEY`,
`DATABENTO_HISTORICAL_OPTIONS_API_KEY`, and a
rotated `DISCORD_BOT_TOKEN`. `DISCORD_CHANNEL_ID` defaults to the production
alert channel. Production pins `ZSCORE_ALERT_THRESHOLD` to `1.2` in the Compose
manifest; change the manifest to deploy a different threshold.
Do not add any credential value to an image, release archive, or GitHub Actions
log.

The container uses `restart: unless-stopped`, so Docker restores it after a host
reboot. The image contains no credentials or market snapshots. Production starts
in cache-only mode so deployments and host restarts do not wait for Databento;
the scheduled refresh service validates and atomically replaces the mounted
cache.

Successful deployments install `option-sniper-refresh.timer`. It runs at 03:15
America/New_York Tuesday through Saturday, with a second trigger at 03:45 as a
retry. Both triggers follow daylight-saving changes. The job refreshes the
shared cache in a temporary container while the public adapter continues
serving, and recreates the adapter only when the cache changed. When the first
run succeeds, the retry sees the current cache and exits without downloading
data. A failed Databento refresh leaves the running service and prior cache
untouched.

`option-sniper-yesterday-refresh.timer` separately runs at 20:20 ET Monday through
Friday, with a retry at 20:40, after the final EQUS.SUMMARY daily publication.
These are 08:20/08:40 UTC+8 the following morning during U.S. daylight saving time
and 09:20/09:40 during standard time. Completion depends on provider availability
and processing time. It calls `refresh-production.sh yesterday`, writes
`/var/lib/sigma-tape/yesterday-state.json`, and recreates only the backend. The
adapter overlays only the saved gainer snapshot when its date is at least as
recent and its screening policy matches. Today's cached universe and alert cycle
remain on the original pre-market schedule. Both refresh jobs share an exclusive
file lock to protect intermediate downloads. Failed refreshes preserve the last
published cache; retries skip historical downloads after a successful run.

Inspect or manually trigger the refresh with:

```bash
sudo systemctl list-timers option-sniper-refresh.timer --no-pager
sudo systemctl status option-sniper-refresh.timer --no-pager
sudo systemctl start option-sniper-refresh.service
sudo journalctl -u option-sniper-refresh.service -n 100 --no-pager
```

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
- `DISCORD_BOT_TOKEN`: bot credential copied into the root-owned EC2 environment
  file during deployment without being printed.

Historical and live Databento keys remain in
`/etc/sigma-tape/sigma-tape.env`; they are not GitHub deployment secrets.

## Reopening after weekends and holidays

`option-sniper-reopening-refresh.timer` checks daily at 19:45 ET, with retries
at 20:00, 20:15, and 20:45 ET. The refresh command uses the exchange calendar:
it only proceeds on the evening immediately before a trading session whose
previous trading session is more than one calendar day earlier. Ordinary
weekday evenings and evenings before holidays are no-ops.

The first check prepares the latest completed-session cache before Blue Ocean
opens at 20:00 ET (09:00 JST in daylight saving time, 10:00 JST in winter).
A valid current cache avoids historical downloads and backend restarts. Failed
refreshes retain the published cache. The existing pre-market timer remains
unchanged and provides a fallback. Reopening refreshes recreate only the backend;
the Discord worker and its pre-market alert-cycle policy remain unchanged.

During that reopening overnight session, Today uses fresh trades for live Z-score
ranking against the last completed-session anchor. Stale trades and trades for
other session dates do not activate the exception. Yesterday's saved display and
ordinary weekday overnight carry behavior are preserved. Live activation depends
on receiving a fresh overnight trade; preparing a cache alone does not create
live prices. The new timer is installed by the deployment script.
