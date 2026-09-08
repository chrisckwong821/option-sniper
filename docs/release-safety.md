# Release readiness and rollback

Both deployment lanes build the candidate before calling `deploy/guarded-activate.sh`.
The guard requires an existing previous release and image, locks the cache against
scheduled refreshes, stops that lane's services, and saves a checksummed snapshot
of version-dependent cache files under `/var/lib/sigma-tape[-staging]-deploy-backups`.
It then recreates only that lane's services with the candidate image.

Readiness requires healthy Docker containers (including the production alerter),
fresh dashboard state, calibration matching the IV reference date, and fresh stock,
option, and IV observations during actual regular exchange hours. Holidays and
early closes are calendar-aware. Static page/assets are checked too. The known
overnight-provider entitlement warning is not a regular-session rollout blocker.

Failure stops candidate writers, validates and restores the cache snapshot, and
recreates the previous image. Newly introduced version-dependent cache files are
removed. Discord's current database and WAL are **never reverted**, so alerts sent
during a failed rollout stay recorded. An SQLite backup of alert history is retained
for disaster recovery only. Unmanaged raw history and other projects are untouched.
Production timer definitions and enabled/active states are restored if timer
installation fails. Credentials are intentionally not rolled back by this guard.

The transaction is fail-closed: if writers cannot stop or cache restore fails,
automatic activation stops and requires operator attention. Successful rollback
still returns failure to GitHub Actions. Cache snapshots are retained for manual
inspection; no automatic backup pruning is performed. Leave space for snapshots
and build layers (the snapshot check preserves at least 1 GiB free).

## Staging rollback drill

Use a distinct staging-only release/image prepared from the tested revision, then
run its guard with `SIGMA_STAGING_FAIL_HEALTH=1`. The failure injection is accepted
only for the staging environment. Verify the command fails, `current` returns to
the prior release, the cache restore succeeds, and container health recovers.
Do not run the production lane as a drill.

Unit tests exercise the shell transaction with mocked Docker/systemd, including
an unhealthy production alerter, modified cache rollback, and alert preservation.
`scripts/preflight_cache_migration.py` separately validates migration on a disposable
production-cache copy, without live feeds, paid downloads, S3 writes, or production
cache changes. A deployment guard does not authorize merging to main or production
deployment; that still requires the user's release approval.
