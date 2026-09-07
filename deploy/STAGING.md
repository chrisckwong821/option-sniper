# Staging deployment

Staging is isolated from production while sharing the same EC2 host and trusted
deployment identity.

| Resource | Production | Staging |
| --- | --- | --- |
| Git branch | `main` | `codex/staging` |
| GitHub environment | `production` | `staging` |
| Release root | `/opt/sigma-tape` | `/opt/sigma-tape-staging` |
| Cache root | `/var/lib/sigma-tape` | `/var/lib/sigma-tape-staging` |
| Environment file | `/etc/sigma-tape/sigma-tape.env` | `/etc/sigma-tape/sigma-tape-staging.env` |
| Compose project | `sigma-tape` | `sigma-tape-staging` |
| Container | `sigma-tape` | `sigma-tape-staging` |
| Loopback port | `8765` | `8766` |
| Public hostname | `optionsniper.xyz` | `staging.optionsniper.xyz` |
| Discord alerter | enabled | omitted |

The GitHub `staging` environment requires `EC2_HOST`, `EC2_USER`,
`EC2_SSH_KEY`, and `EC2_KNOWN_HOSTS`. It deliberately has no Discord token.

The first deployment copies only the production read-model caches into the
separate staging cache. It never copies the Discord alert database. Later
deployments retain staging's own cache and can be rolled back independently.

The existing remotely managed Cloudflare tunnel must have this public-hostname
route:

```text
staging.optionsniper.xyz -> http://127.0.0.1:8766
```

The EC2 security group should continue to deny public ingress to both loopback
ports. Cloudflare Tunnel remains the only public route.
