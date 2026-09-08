"""Inventory and publish downloaded market history; excludes credentials and runtime files."""
import gzip
import argparse
import json
from pathlib import Path
try:
    from .shared_cache import SharedCache, atomic_json, digest
except ImportError:
    from shared_cache import SharedCache, atomic_json, digest


def inventory(root):
    import databento as db
    root = Path(root).resolve()
    files = []
    paths = list((root / 'iv-expansion-history' / 'raw').rglob('*.dbn.zst'))
    paths += list((root / 'requests').glob('*.dbn.zst'))
    paths += list((root / 'refresh').glob('*/option-quotes/chunk-*.json'))
    paths += list((root / 'refresh').glob('*/nasdaq-universe.json'))
    paths += list((root / 'symbology').glob('*.json'))
    for path in sorted(paths):
        if path.is_symlink() or not path.stat().st_size:
            continue
        row = {'path': path.relative_to(root).as_posix(), 'bytes': path.stat().st_size, 'sha256': digest(path)}
        if path.name.endswith('.dbn.zst'):
            try:
                meta = db.DBNStore.from_file(path).metadata
            except Exception as exc:
                row['invalid'] = type(exc).__name__
                files.append(row)
                continue
            row['request'] = {'dataset': meta.dataset, 'schema': str(meta.schema), 'stype_in': str(meta.stype_in),
                              'start': meta.start, 'end': meta.end, 'symbols': sorted(meta.symbols), 'limit': meta.limit}
        else:
            row['request'] = json.loads(path.read_text()).get('request')
        files.append(row)
    return {'schema_version': 1, 'files': files}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=Path('.cache/sigma-tape'))
    p.add_argument('--upload', action='store_true')
    args = p.parse_args()
    manifest = inventory(args.root)
    out = args.root / 'history-manifest.json'
    atomic_json(out, manifest)
    print(json.dumps({'files': len(manifest['files']), 'bytes': sum(f['bytes'] for f in manifest['files'])}), flush=True)
    if args.upload:
        shared = SharedCache(args.root)
        if not shared.enabled:
            raise RuntimeError('Set SIGMA_S3_BUCKET before uploading')
        for entry in manifest['files']:
            shared.put('history/' + entry['path'], args.root / entry['path'])
        compressed = out.with_suffix('.json.gz')
        with out.open('rb') as source, gzip.open(compressed, 'wb') as target:
            import shutil
            shutil.copyfileobj(source, target)
        shared.put('history/manifest.json.gz', compressed)


if __name__ == '__main__':
    main()
