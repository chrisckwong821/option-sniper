"""Prepare an allowlisted, checksummed startup snapshot for private S3 storage.

This command only stages local files. It never uploads or downloads market data.
"""
from __future__ import annotations
from contextlib import closing
import argparse
import gzip
import hashlib
import json
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .dashboard_state import DEFAULT_CACHE_ROOT, validate_bundle

JSON_FILES = (
    'dashboard-state.json',
    'live-iv-expansion.json',
    'iv-expansion-statistics-session-batch.json',
)
HISTORY_FILE = 'iv-expansion-history-session-batch.sqlite3'


def prepare(cache_root: Path, output: Path) -> dict:
    # Validate all inputs before creating a release directory.
    data = {name: json.loads((cache_root / name).read_text()) for name in JSON_FILES if (cache_root / name).exists()}
    state = validate_bundle(data['dashboard-state.json'])
    stats = data.get('iv-expansion-statistics-session-batch.json', {})
    if not stats.get('statistics') or stats.get('last_calibration_session') != state['today']['anchor_date']:
        data.pop('iv-expansion-statistics-session-batch.json', None)
        stats = {}
    live = data.get('live-iv-expansion.json', {})
    expected_prefix = f"{state['generated_at_utc']}|{state['today']['anchor_date']}|"
    if not live.get('state_key', '').startswith(expected_prefix):
        data.pop('live-iv-expansion.json', None)
    output.mkdir(parents=True, exist_ok=False)
    entries = []
    for name in data:
        raw = json.dumps(data[name], separators=(',', ':'), allow_nan=False).encode()
        encoded = gzip.compress(raw, mtime=0)
        target = output / (name + '.gz')
        target.write_bytes(encoded)
        entries.append({'name': name, 'object': target.name, 'bytes': len(encoded),
                        'sha256': hashlib.sha256(encoded).hexdigest(),
                        'uncompressed_bytes': len(raw)})
    # SQLite backup captures committed data safely, including WAL contents.
    source_path = cache_root / HISTORY_FILE
    if source_path.exists():
        with tempfile.TemporaryDirectory() as folder:
            backup = Path(folder) / HISTORY_FILE
            with closing(sqlite3.connect(source_path.resolve().as_uri() + '?mode=ro', uri=True)) as source:
                with closing(sqlite3.connect(backup)) as destination:
                    source.backup(destination)
                    if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise ValueError('History database failed integrity check')
            raw = backup.read_bytes()
            encoded = gzip.compress(raw, mtime=0)
            target = output / (HISTORY_FILE + '.gz')
            target.write_bytes(encoded)
            entries.append({'name': HISTORY_FILE, 'object': target.name, 'bytes': len(encoded),
                            'sha256': hashlib.sha256(encoded).hexdigest(),
                            'uncompressed_bytes': len(raw)})
    manifest = {
        'schema_version': 1,
        'created_at': datetime.now(UTC).isoformat(),
        'completed_session': state['snapshot']['data_date'],
        'screening_policy_fingerprint': state.get('screening_policy_fingerprint'),
        'state_generation': state.get('generated_at_utc'),
        'calibration_holdout_session': stats.get('holdout_session'),
        'calibration_first_session': stats.get('first_calibration_session'),
        'calibration_last_session': stats.get('last_calibration_session'),
        'files': entries,
    }
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-root', type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    manifest = prepare(args.cache_root, args.output)
    print(json.dumps({'output': str(args.output), 'completed_session': manifest['completed_session'],
                      'files': len(manifest['files']),
                      'compressed_bytes': sum(f['bytes'] for f in manifest['files'])}, indent=2))


if __name__ == '__main__':
    main()
