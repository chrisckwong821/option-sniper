"""Read-only background calibration updates; activation stays on the event loop."""
import asyncio
import gzip
import hashlib
import json
import math
import tempfile
from datetime import date, datetime, UTC
from pathlib import Path

from .iv_bucket_score import BucketScores
from .shared_cache import SharedCache

METHOD = 'moneyness-matched-black76-robust-mad-min2dte-v2'
NAME = 'iv-expansion-statistics-session-batch.json'


def validate(raw, anchor, fingerprint):
    payload = json.loads(raw)
    last = date.fromisoformat(payload['last_calibration_session'])
    if last > date.fromisoformat(anchor):
        raise ValueError('Deferred: calibration requires newer daily IV references')
    if payload.get('months') != 6 or not payload.get('statistics'):
        raise ValueError('Invalid calibration window or empty statistics')
    if payload.get('screening_policy_fingerprint', fingerprint) != fingerprint:
        raise ValueError('Incompatible screening policy')
    generated = datetime.fromisoformat(payload['generated_at_utc'])
    if generated.tzinfo is None:
        raise ValueError('Calibration timestamp must include timezone')
    keys = set()
    for row in payload['statistics']:
        key = (row['ticker'], row['bucket'], row['tenor_class'])
        if key in keys or row.get('method') != METHOD:
            raise ValueError('Duplicate distribution or incompatible method')
        keys.add(key)
        if row.get('calibration_excludes_session') != last.isoformat():
            raise ValueError('Invalid holdout session')
        if row['bucket'] not in {f'{m//60:02}:{m%60:02}' for m in range(570, 960, 30)}:
            raise ValueError('Invalid time bucket')
        dist = row['log_iv_change_distribution']
        if not isinstance(dist.get('n'), int) or dist['n'] < 0:
            raise ValueError('Invalid observation count')
        if dist.get('status') == 'valid':
            if not all(isinstance(dist.get(k), (int, float)) and math.isfinite(dist[k]) for k in ('median', 'robust_sigma')) or dist['robust_sigma'] <= 0:
                raise ValueError('Invalid calibration distribution')
    return payload, (last, generated.astimezone(UTC)), hashlib.sha256(raw).hexdigest()


class CalibrationRefresh:
    def __init__(self, store, root, anchor, fingerprint, interval=300, shared=None):
        self.store, self.root = store, Path(root)
        self.anchor, self.fingerprint = anchor, fingerprint
        self.interval = interval
        self.shared = shared or SharedCache(root)
        self.remote_release = None
        self.active_order = None
        self.version = None
        self.status = {'active_date': store.iv_tracker.bucket_scores.last_session,
                       'active_version': None, 'last_checked_at': None,
                       'last_outcome': 'startup', 'error': None}
        try:
            _, self.active_order, self.version = validate((self.root / NAME).read_bytes(), anchor, fingerprint)
            self.status['active_version'] = self.version
            store.iv_tracker.bucket_scores.version = self.version
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def candidate(self):
        # All filesystem/network work happens off the serving event loop.
        choices, errors = [], []
        local = self.root / NAME
        if local.exists():
            try:
                raw = local.read_bytes()
                payload, order, version = validate(raw, self.anchor, self.fingerprint)
                choices.append((order, version, payload, 'local'))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(str(exc))
        if self.shared.enabled:
            try:
                with tempfile.TemporaryDirectory() as folder:
                    folder = Path(folder)
                    pointer = folder / 'latest.json'
                    if self.shared.get('computed/latest.json', pointer):
                        release = json.loads(pointer.read_text())['release']
                        if release != self.remote_release:
                            manifest_file = folder / 'manifest.json'
                            if not self.shared.get(f'{release}/manifest.json', manifest_file):
                                raise ValueError('Missing published manifest')
                            manifest = json.loads(manifest_file.read_text())
                            if manifest.get('schema_version') != 1 or manifest.get('screening_policy_fingerprint') != self.fingerprint:
                                raise ValueError('Incompatible computed manifest')
                            entries = [e for e in manifest['files'] if e['name'] == NAME]
                            if len(entries) != 1:
                                raise ValueError('Missing or duplicate calibration entry')
                            entry = entries[0]
                            if not 0 < int(entry['bytes']) <= 64 * 1024 * 1024:
                                raise ValueError('Calibration download exceeds size limit')
                            compressed = folder / 'statistics.gz'
                            if not self.shared.get(f'{release}/{entry["object"]}', compressed, entry['sha256']):
                                raise ValueError('Missing published calibration')
                            if compressed.stat().st_size != entry['bytes']:
                                raise ValueError('Calibration compressed size mismatch')
                            limit = min(int(entry['uncompressed_bytes']), 64 * 1024 * 1024)
                            with gzip.open(compressed, 'rb') as stream:
                                raw = stream.read(limit + 1)
                            if len(raw) != entry['uncompressed_bytes'] or len(raw) > limit:
                                raise ValueError('Calibration uncompressed size mismatch')
                            payload, order, version = validate(raw, self.anchor, self.fingerprint)
                            if payload['last_calibration_session'] != manifest['completed_session']:
                                raise ValueError('Calibration date disagrees with manifest')
                            choices.append((order, version, payload, 's3'))
                            self.remote_release = release
            except Exception as exc:
                errors.append(str(exc))
        return choices, errors

    async def check(self):
        self.status['last_checked_at'] = datetime.now(UTC).isoformat()
        try:
            choices, errors = await asyncio.to_thread(self.candidate)
            choices = [c for c in choices if self.active_order is None or c[0] > self.active_order]
            if not choices:
                self.status.update(last_outcome='retained' if errors else 'unchanged', error='; '.join(errors) or None)
                return False
            order, version, payload, source = max(choices, key=lambda c: c[0])
            scorer = BucketScores.from_payload(payload, version)
            # One pointer swap; readers see either the complete old or new scorer.
            self.store.iv_tracker.bucket_scores = scorer
            self.active_order, self.version = order, version
            self.store.dirty_symbols.update(self.store.iv_tracker.results)
            self.store.invalidate_snapshot()
            self.status.update(active_date=scorer.last_session, active_version=version,
                               last_outcome='activated', source=source, error='; '.join(errors) or None)
            return True
        except Exception as exc:
            self.status.update(last_outcome='retained', error=str(exc))
            return False

    async def run(self, stop):
        while not stop.is_set():
            await self.check()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
