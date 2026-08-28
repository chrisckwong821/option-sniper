# EC2 runtime

Sigma Tape runs as a non-root Docker container and binds only to the EC2
loopback interface. A reverse proxy or Cloudflare Tunnel should be the only
public path to the service.

Host-owned runtime paths:

- `/opt/sigma-tape`: release source and Compose definition
- `/etc/sigma-tape/sigma-tape.env`: Databento credentials, root-owned `0600`
- `/var/lib/sigma-tape`: private dashboard and live-price cache, UID `10001`

Start or update the backend from `/opt/sigma-tape`:

```bash
sudo docker compose -f deploy/compose.yml build
sudo docker compose -f deploy/compose.yml up -d
sudo docker compose -f deploy/compose.yml ps
curl --fail http://127.0.0.1:8765/api/health
```

The container uses `restart: unless-stopped`, so Docker restores it after a host
reboot. The image contains no credentials or market snapshots. A mounted cache
is accepted only when it matches the latest completed Nasdaq session; otherwise
the backend performs its bounded historical refresh before opening the server.
