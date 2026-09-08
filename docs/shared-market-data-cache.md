# Shared market-data cache

The application tries a valid local computed state, then `computed/latest.json`
in the private S3 prefix. A compatible release restores dashboard state, IV marks,
bucket calibration, and historical observations when available. Releases are
published with their manifest last, followed by the latest pointer. IV marks from
another state generation and calibration from another session are not published
with a new dashboard state.

When computation is needed, historical requests first reuse local DBN coverage.
They next fetch missing S3 coverage, matching dataset, schema, symbology, symbols,
and exclusive time bounds. Only remaining gaps can reach Databento. Different
schemas or datasets are never silently substituted. Parent-symbol definitions
currently require the same parent set for reuse. Cached option-quote chunks and
universe inputs also restore by their existing deterministic paths.

S3 404 is a miss. Permission, network, malformed-manifest and checksum failures
stop the reconstruction path. Existing dashboard state can still be served with
an explicit cache-fallback error. Partial or undecodable archive files are listed
as invalid and excluded from coverage planning.

New Databento responses are saved atomically in `requests/`, indexed locally by
request metadata, and uploaded to the same deterministic S3 key. Startup
subprocesses share a locked purchase-reservation file: total estimated purchases
are capped at $5, including failed purchases. Missing data spanning more than
seven calendar days, or older than seven days before the target session, is
blocked. `SIGMA_RECONSTRUCTION_MAX_COST` can lower the cap. `--refresh-mode never`
still prohibits paid startup reconstruction; S3 restoration remains available.

Bucket statistics use a bounded six-month calibration window, excluding the
holdout observation. Missing/stale calibration triggers the history builder,
which reuses its observation database and the same raw-history hierarchy.

## Configuration

Set `SIGMA_S3_BUCKET`, `SIGMA_S3_REGION` and optionally `SIGMA_S3_PREFIX`.
On the Mac, use `SIGMA_AWS_PROFILE=option-sniper` after
`aws login --profile option-sniper --region ap-southeast-1`.
On EC2, omit the profile and use an instance role with ListBucket restricted to
the prefix, plus GetObject/PutObject and AbortMultipartUpload within that prefix. The bucket stays private.
Browser credentials expire and require a new login; they are not a server identity.

Alternatively, local non-secret settings can live in
`.cache/sigma-tape/shared-cache-config.json` with `bucket`, `region`, and `profile`.
This file is not uploaded. No credentials, environment files or running process
state are archived.

## Archive commands

`python -m scripts.archive_history` inventories local raw DBN files, request
caches, and historical option-quote/universe inputs into `history-manifest.json`.
Add `--upload` to upload those files and publish the manifest after completion.
The initial inventory also retains hashes of undecodable files for accounting,
but does not advertise them as reusable market-data coverage.

The initial computed release is `20260908-startup-v1/`. Raw history is stored
under `history/`, preserving paths relative to the application cache root.
Published archives and future computed releases require a retention policy if
you want automatic deletion; this implementation never deletes S3 data.

Each new raw request publishes an individual metadata sidecar after its data
object. Reconstruction discovers these sidecars as well as the initial archive
manifest, so new overlapping requests can reuse data uploaded by another machine
without racing to overwrite one shared index file.

The initial remote index is compressed as `history/manifest.json.gz`. Symbol
resolution metadata follows the same local/S3/provider ordering in `symbology/`.
Use `python -m scripts.verify_history_archive --publish` after an initial upload
to check every file against S3's full-object CRC64 checksum and the local SHA-256
inventory before publishing the index.
