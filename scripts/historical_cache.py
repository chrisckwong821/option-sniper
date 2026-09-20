"""Cache-aware Databento access with interval reuse and a shared per-process $5 cap."""
from __future__ import annotations
import gzip
import hashlib
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import databento as db
import pandas as pd
try:
    from .shared_cache import SharedCache, SharedCacheError, atomic_json, digest
except ImportError:
    from shared_cache import SharedCache, SharedCacheError, atomic_json, digest

_LOCK = threading.RLock()
_SPENT = 0.0
_LOCAL_MANIFESTS = {}
_SHARED_INDEXES = {}


def normalized(request):
    symbols = request.get('symbols', 'ALL_SYMBOLS')
    if isinstance(symbols, str):
        symbols = [symbols]
    return {'dataset': str(request['dataset']), 'schema': str(request['schema']),
            'stype_in': str(request.get('stype_in', 'raw_symbol')),
            'symbols': sorted(symbols), 'start': pd.Timestamp(request['start']).value,
            'end': pd.Timestamp(request['end']).value, 'limit': request.get('limit')}


def _read_frame(store, schema):
    if schema != 'definition':
        return store.to_df()
    # The historical-cache assembly path must also avoid wide OPRA frames.
    chunks = store.to_df(count=10000, map_symbols=False)
    if isinstance(chunks, pd.DataFrame):
        chunks = [chunks]
    reduced = []
    for chunk in chunks:
        reduced.append(chunk[[c for c in ('raw_symbol', 'symbol') if c in chunk]].copy())
    return pd.concat(reduced) if reduced else pd.DataFrame()


class FrameStore:
    def __init__(self, frame):
        self.frame = frame

    def to_df(self, **kwargs):
        return self.frame.copy()


class HistoricalCache:
    def __init__(self, api_key=None, root=None, client=None, shared=None):
        self.root = Path(root or os.environ.get('SIGMA_CACHE_ROOT', '.cache/sigma-tape')).resolve()
        self.shared = shared or SharedCache(self.root)
        self.api_key = api_key
        self._client = client
        self.timeseries = SimpleNamespace(get_range=self.get_range)
        self.metadata = SimpleNamespace(get_cost=self.get_cost)
        self.remote_loaded = False
        self.download_cost = 0.0
        self.download_count = 0
        self.entries = []
        self._indexed_count = -1
        self._coverage_index = {}
        manifest = self.root / 'history-manifest.json'
        if manifest.exists():
            signature = (manifest.stat().st_mtime_ns, manifest.stat().st_size)
            cached = _LOCAL_MANIFESTS.get(str(manifest))
            if cached is None or cached[0] != signature:
                cached = (signature, json.loads(manifest.read_text()).get('files', []))
                _LOCAL_MANIFESTS[str(manifest)] = cached
            self.entries = cached[1]
        if not self.entries and (self.root / 'iv-expansion-history' / 'raw').exists():
            try:
                from .archive_history import inventory
            except ImportError:
                from archive_history import inventory
            payload = inventory(self.root)
            atomic_json(manifest, payload)
            self.entries = payload['files']
        # Newly purchased requests need no archive rebuild to be reused locally.
        for sidecar in (self.root / 'requests').glob('*.json'):
            self.entries.append(json.loads(sidecar.read_text()))

    @property
    def client(self):
        if self._client is None:
            if not self.api_key:
                raise RuntimeError('Historical data is missing locally and in S3; a Databento key is required for the remaining gaps')
            self._client = db.Historical(self.api_key)
        return self._client

    @property
    def symbology(self):
        return SimpleNamespace(resolve=self.resolve_symbols)

    def resolve_symbols(self, **request):
        key = hashlib.sha256(json.dumps(request, sort_keys=True, default=str).encode()).hexdigest()
        target = self.root / 'symbology' / (key + '.json')
        if not target.exists():
            if not self.shared.get('history/symbology/' + target.name, target):
                payload = self.client.symbology.resolve(**request)
                atomic_json(target, payload)
                self.shared.put('history/symbology/' + target.name, target)
        return json.loads(target.read_text())

    def _path(self, request):
        key = hashlib.sha256(json.dumps(normalized(request), sort_keys=True).encode()).hexdigest()
        return self.root / 'requests' / (key + '.dbn.zst')

    def _remote_index(self):
        if self.remote_loaded or not self.shared.enabled:
            return
        target = self.root / 'remote-history-manifest.json'
        compressed = target.with_suffix('.json.gz')
        found = self.shared.get('history/manifest.json.gz', compressed)
        if found:
            with gzip.open(compressed, 'rt') as stream:
                incoming = json.load(stream)
            atomic_json(target, incoming)
        elif self.shared.get('history/manifest.json', target):
            incoming = json.loads(target.read_text())
            found = True
        if found:
            if incoming.get('schema_version') != 1:
                raise SharedCacheError('Unsupported history manifest')
            known = {entry['path'] for entry in self.entries}
            self.entries.extend(entry for entry in incoming.get('files', []) if entry['path'] not in known)
        for key in self.shared.list_keys('history/requests/'):
            if not key.endswith('.json'):
                continue
            target = self.root / 'remote-request-index' / Path(key).name
            if not self.shared.get(key, target):
                raise SharedCacheError('Request index changed during reconstruction: ' + key)
            entry = json.loads(target.read_text())
            if not any(existing['path'] == entry['path'] for existing in self.entries):
                self.entries.append(entry)
        self.remote_loaded = True

    def _index(self):
        if self._indexed_count == len(self.entries):
            return
        cached_index = _SHARED_INDEXES.get(str(self.root))
        signature = (id(self.entries), len(self.entries))
        if cached_index is not None and cached_index[0] == signature:
            self._coverage_index = cached_index[1]
            self._indexed_count = len(self.entries)
            return
        self._coverage_index = {}
        unique = {entry['path']: entry for entry in self.entries}
        for entry in unique.values():
            r = entry.get('request')
            if not r or entry.get('invalid') or not entry['path'].endswith('.dbn.zst'):
                continue
            for symbol in r.get('symbols', []):
                key = (r.get('dataset'), r.get('schema'), r.get('stype_in'), symbol)
                self._coverage_index.setdefault(key, []).append(entry)
        self._indexed_count = len(self.entries)
        _SHARED_INDEXES[str(self.root)] = (signature, self._coverage_index)

    def _parts(self, request, remote=False):
        wanted = normalized(request)
        self._index()
        if wanted['limit']:
            return [], [(wanted['start'], wanted['end'], wanted['symbols'])]
        parts, gaps = [], []
        for symbol in wanted['symbols']:
            remaining = [(wanted['start'], wanted['end'])]
            key = (wanted['dataset'], wanted['schema'], wanted['stype_in'], symbol)
            for entry in self._coverage_index.get(key, []):
                r = entry.get('request')
                if entry.get('invalid') or not r or not entry['path'].endswith('.dbn.zst'):
                    continue
                if any(r.get(k) != wanted[k] for k in ('dataset', 'schema', 'stype_in')) or r.get('limit'):
                    continue
                # Parent symbols expand into contracts; only reuse matching parent sets.
                if wanted['stype_in'] != 'raw_symbol' and sorted(r['symbols']) != wanted['symbols']:
                    continue
                if not isinstance(r.get('start'), int) or not isinstance(r.get('end'), int):
                    continue
                if r['end'] <= wanted['start'] or r['start'] >= wanted['end']:
                    continue
                path = (self.root / entry['path']).resolve()
                if not path.is_relative_to(self.root):
                    raise SharedCacheError('Unsafe history manifest path')
                if not remote and not path.exists():
                    continue
                new_remaining = []
                for a, b in remaining:
                    lo, hi = max(a, r['start']), min(b, r['end'])
                    if lo >= hi:
                        new_remaining.append((a, b)); continue
                    parts.append((entry, symbol, lo, hi))
                    if a < lo: new_remaining.append((a, lo))
                    if hi < b: new_remaining.append((hi, b))
                remaining = new_remaining
                if not remaining: break
            gaps.extend((a, b, symbol) for a, b in remaining)
        grouped = {}
        for a, b, symbol in gaps:
            grouped.setdefault((a, b), []).append(symbol)
        return parts, [(a, b, symbols) for (a, b), symbols in grouped.items()]

    def resolve(self, request):
        path = self._path(request)
        if path.exists():
            return path, [], []
        parts, gaps = self._parts(request)
        if not gaps:
            return None, parts, gaps
        # Exact new requests are published at deterministic keys.
        relative = path.relative_to(self.root).as_posix()
        if self.shared.get('history/' + relative, path):
            db.DBNStore.from_file(path)  # Decode before treating it as a hit.
            return path, [], []
        self._remote_index()
        parts, gaps = self._parts(request, remote=True)
        for entry, _, _, _ in parts:
            target = self.root / entry['path']
            if not target.exists() and not self.shared.get('history/' + entry['path'], target, entry.get('sha256')):
                raise SharedCacheError('History manifest references a missing object: ' + entry['path'])
        return None, parts, gaps

    def _requests(self, original, gaps):
        if gaps:
            start, end = min(g[0] for g in gaps), max(g[1] for g in gaps)
            if end - start > pd.Timedelta(days=7).value:
                raise RuntimeError('Missing history spans more than one week; explicit approval is required before Databento downloads')
        return [{**original, 'symbols': symbols, 'start': pd.Timestamp(a, tz='UTC').isoformat(),
                 'end': pd.Timestamp(b, tz='UTC').isoformat()} for a, b, symbols in gaps]

    def get_cost(self, **request):
        path, parts, gaps = self.resolve(request)
        return sum(float(self.client.metadata.get_cost(**r)) for r in self._requests(request, gaps))

    def get_range(self, **request):
        destination = request.pop('path', None)
        if destination is not None:
            raise ValueError('Use DownloadLedger for requests with explicit output paths')
        with _LOCK:
            path, parts, gaps = self.resolve(request)
            if path:
                return db.DBNStore.from_file(path)
            frames, loaded = [], {}
            grouped = {}
            for entry, symbol, a, b in parts:
                key = (entry['path'], a, b)
                if key not in grouped:
                    grouped[key] = (entry, [])
                grouped[key][1].append(symbol)
            for (name, a, b), (entry, symbols) in grouped.items():
                if name not in loaded:
                    source = self.root / name
                    if entry.get('sha256') and digest(source) != entry['sha256']:
                        raise SharedCacheError('Local history checksum mismatch: ' + name)
                    loaded[name] = _read_frame(db.DBNStore.from_file(source), request.get('schema'))
                frame = loaded[name]
                stamp = pd.to_datetime(frame.index, utc=True)
                mask = (stamp >= pd.Timestamp(a, tz='UTC')) & (stamp < pd.Timestamp(b, tz='UTC'))
                if request.get('stype_in', 'raw_symbol') == 'raw_symbol' and 'ALL_SYMBOLS' not in symbols:
                    if 'symbol' not in frame.columns:
                        raise SharedCacheError('Cannot safely filter historical symbols: ' + name)
                    mask &= frame['symbol'].isin(symbols)
                frames.append(frame.loc[mask])
            for missing in self._requests(request, gaps):
                cost = float(self.client.metadata.get_cost(**missing))
                cap = min(5.0, float(os.environ.get('SIGMA_RECONSTRUCTION_MAX_COST') or '5'))
                if not 0 <= cost <= cap:
                    raise RuntimeError(f'Databento request exceeds ${cap:.4f} reconstruction budget')
                try:
                    from .reconstruction_budget import reserve
                except ImportError:
                    from reconstruction_budget import reserve
                reserve(cost, missing)
                self.download_cost += cost
                self.download_count += 1
                target = self._path(missing)
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix('.part')
                temporary.unlink(missing_ok=True)
                try:
                    self.client.timeseries.get_range(**missing, path=temporary)
                    store = db.DBNStore.from_file(temporary)
                    frames.append(_read_frame(store, request.get('schema')))
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
                entry = {'path': target.relative_to(self.root).as_posix(), 'request': normalized(missing),
                         'bytes': target.stat().st_size, 'sha256': digest(target)}
                atomic_json(target.with_suffix('.json'), entry)
                self.entries.append(entry)
                self.shared.put('history/' + entry['path'], target)
                sidecar = target.with_suffix('.json')
                self.shared.put('history/' + sidecar.relative_to(self.root).as_posix(), sidecar)
            if not frames:
                return FrameStore(pd.DataFrame())
            # A parent request can contribute the same frame through several parents.
            combined = pd.concat(frames).reset_index()
            keys = [combined.columns[0]] + [k for k in ("instrument_id", "symbol", "sequence") if k in combined.columns]
            combined = combined.drop_duplicates(subset=keys)
            return FrameStore(combined.set_index(combined.columns[0]).sort_index())
