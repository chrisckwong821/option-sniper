"""Retrieve a validated computed bundle before any historical reconstruction."""
from contextlib import closing
import gzip
import json
import os
import shutil
import tempfile
from pathlib import Path
from .shared_cache import SharedCache, SharedCacheError, atomic_json

ALLOWED = {'dashboard-state.json', 'live-iv-expansion.json',
           'iv-expansion-statistics-session-batch.json', 'iv-expansion-history-session-batch.sqlite3'}


def restore(root, completed_session, fingerprint, *, include_state=True, history_only=False):
    shared = SharedCache(root)
    if not shared.enabled:
        return False
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as folder:
        stage = Path(folder)
        pointer = stage / 'latest.json'
        if not shared.get('computed/latest.json', pointer):
            return False
        release = json.loads(pointer.read_text())['release']
        manifest_path = stage / 'manifest.json'
        if not shared.get(f'{release}/manifest.json', manifest_path):
            raise SharedCacheError('Published computed bundle has no manifest')
        manifest = json.loads(manifest_path.read_text())
        if manifest.get('schema_version') != 1:
            raise SharedCacheError('Unsupported computed manifest')
        if not history_only and (manifest.get('completed_session') != completed_session or manifest.get('screening_policy_fingerprint') != fingerprint):
            return False
        entries = manifest['files']
        if not {e['name'] for e in entries}.issubset(ALLOWED) or 'dashboard-state.json' not in {e['name'] for e in entries} or len({e['name'] for e in entries}) != len(entries):
            raise SharedCacheError('Unexpected files in computed bundle')
        for entry in entries:
            name = entry['name']
            if history_only and name not in ('iv-expansion-history-session-batch.sqlite3', 'iv-expansion-statistics-session-batch.json'):
                continue
            if not include_state and (name == 'dashboard-state.json' or (name != 'iv-expansion-statistics-session-batch.json' and (root / name).exists())):
                continue
            compressed = stage / (name + '.gz')
            if not shared.get(f'{release}/{entry["object"]}', compressed, entry['sha256']):
                raise SharedCacheError('Published computed bundle is incomplete')
            if compressed.stat().st_size != entry['bytes']:
                raise SharedCacheError('Compressed size mismatch')
            target = stage / name
            with gzip.open(compressed, 'rb') as source, target.open('wb') as output:
                remaining = min(int(entry['uncompressed_bytes']), 512 * 1024 * 1024)
                while block := source.read(min(1024 * 1024, remaining + 1)):
                    remaining -= len(block)
                    if remaining < 0:
                        raise SharedCacheError('Computed file exceeds declared size')
                    output.write(block)
            if target.stat().st_size != entry['uncompressed_bytes']:
                raise SharedCacheError('Uncompressed size mismatch')
        if history_only:
            calibration = stage / 'iv-expansion-statistics-session-batch.json'
            if not calibration.exists():
                raise SharedCacheError('Historical database has no matching method metadata')
            methods = {row.get('method') for row in json.loads(calibration.read_text()).get('statistics', [])}
            if methods != {'moneyness-matched-black76-robust-mad-min2dte-v2'}:
                raise SharedCacheError('Historical database calibration method is incompatible')
        state = stage / 'dashboard-state.json'
        if state.exists():
            from .dashboard_state import validate_bundle, has_complete_prior_session_metrics, RefreshPlan
            payload = validate_bundle(json.loads(state.read_text()))
            if payload['snapshot']['data_date'] != completed_session or payload['today']['anchor_date'] != completed_session or payload.get('screening_policy_fingerprint') != fingerprint:
                raise SharedCacheError('Computed state disagrees with manifest')
            if not has_complete_prior_session_metrics(payload, RefreshPlan(**payload['refresh_plan'])):
                raise SharedCacheError('Computed state has incomplete metrics')
        database = stage / 'iv-expansion-history-session-batch.sqlite3'
        if database.exists():
            import sqlite3
            with closing(sqlite3.connect(database)) as connection:
                if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise SharedCacheError('Restored history database is invalid')
        for name in sorted(ALLOWED - {'dashboard-state.json'}):
            source = stage / name
            if source.exists():
                # Preserve locally accumulated observations.
                if name.endswith('.sqlite3') and (root / name).exists():
                    continue
                os.replace(source, root / name)
        if state.exists():
            os.replace(state, root / state.name)
    return True


def publish(root):
    from .prepare_shared_cache import prepare
    from datetime import datetime, UTC
    shared = SharedCache(root)
    if not shared.enabled or not (Path(root) / 'dashboard-state.json').exists():
        return
    release = 'computed/' + datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
    with tempfile.TemporaryDirectory(dir=root) as folder:
        output = Path(folder) / 'bundle'
        manifest = prepare(Path(root), output)
        for entry in manifest['files']:
            shared.put(f'{release}/{entry["object"]}', output / entry['object'])
        shared.put(f'{release}/manifest.json', output / 'manifest.json')
        atomic_json(output / 'latest.json', {'release': release})
        shared.put('computed/latest.json', output / 'latest.json')
