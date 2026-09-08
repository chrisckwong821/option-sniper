"""Verify an S3 cache migration on a disposable copy, never on the source cache."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3

FILES = ('dashboard-state.json', 'yesterday-state.json', 'live-prices.json',
         'live-iv-expansion.json', 'option-contracts.json')


def clone_cache(source: Path, destination: Path) -> dict:
    source, destination = source.resolve(), destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError('Source and destination must be separate directories')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Destination must be empty')
    destination.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in FILES:
        if (source / name).is_file():
            shutil.copy2(source / name, destination / name)
            copied.append(name)
    # Snapshot SQLite through its backup API so committed WAL transactions are included.
    alert = source / 'discord-alerts.sqlite3'
    if alert.exists():
        with closing(sqlite3.connect(alert.as_uri() + '?mode=ro', uri=True)) as original:
            with closing(sqlite3.connect(destination / alert.name)) as backup:
                original.backup(backup)
        copied.append(alert.name)
    return {'copied_files': copied}


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    source, root = args.source.resolve(), args.destination.resolve()
    # No paid data or Discord calls are allowed in this preflight.
    for key in list(os.environ):
        if key.startswith(('DATABENTO_', 'DISCORD_')):
            del os.environ[key]
    os.environ['SIGMA_RECONSTRUCTION_MAX_COST'] = '0'
    from scripts.shared_cache import SharedCache
    def deny_upload(*args, **kwargs):
        raise RuntimeError('Preflight prohibits S3 uploads')
    SharedCache.put = deny_upload
    if shutil.disk_usage(root.parent).free < 1024 ** 3:
        raise RuntimeError('At least 1 GiB free is required for preflight')
    source_before = checksum(source / 'dashboard-state.json')
    report = clone_cache(source, root)
    alert_before = checksum(root / 'discord-alerts.sqlite3')
    old = json.loads((root / 'dashboard-state.json').read_text())
    from scripts.dashboard_state import screening_policy_fingerprint
    from scripts.computed_cache import restore
    target = old['today']['anchor_date']
    fingerprint = screening_policy_fingerprint()
    if not restore(root, target, fingerprint):
        raise RuntimeError('No compatible published S3 bundle for production anchor')
    migrated = json.loads((root / 'dashboard-state.json').read_text())
    from scripts.calibration_refresh import validate, NAME
    calibration, _, version = validate((root / NAME).read_bytes(), target, fingerprint)
    with closing(sqlite3.connect(root / 'iv-expansion-history-session-batch.sqlite3')) as db:
        integrity = db.execute('PRAGMA quick_check').fetchone()[0]
    if integrity != 'ok':
        raise RuntimeError('Historical database integrity check failed')
    from scripts.live_price_adapter import create_app, parse_args
    import sys
    sys.argv = ['preflight', '--refresh-mode', 'never',
                '--state-cache', str(root / 'dashboard-state.json'),
                '--yesterday-state-cache', str(root / 'yesterday-state.json'),
                '--cache', str(root / 'live-prices.json'),
                '--iv-cache', str(root / 'live-iv-expansion.json'),
                '--option-catalog-cache', str(root / 'option-contracts.json')]
    create_app(parse_args())  # Do not start app tasks, feeds, or the alerter.
    assert checksum(root / 'discord-alerts.sqlite3') == alert_before, 'Alert snapshot changed'
    assert checksum(source / 'dashboard-state.json') == source_before, 'Source state changed during test'
    report.update(status='passed', anchor=target,
                  old_policy=old.get('screening_policy_fingerprint'),
                  new_policy=migrated['screening_policy_fingerprint'],
                  calibration_date=calibration['last_calibration_session'],
                  calibration_version=version,
                  distributions=len(calibration['statistics']),
                  history_integrity=integrity,
                  alert_snapshot_preserved=True, source_state_unchanged=True,
                  cache_bytes=sum(p.stat().st_size for p in root.rglob('*') if p.is_file()))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
