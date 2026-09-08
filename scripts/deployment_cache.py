"""Checkpoint only version-dependent cache files; never roll back alert history."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

FILES = ('dashboard-state.json', 'yesterday-state.json', 'live-prices.json',
         'live-iv-expansion.json', 'option-contracts.json',
         'iv-expansion-statistics-session-batch.json',
         'iv-expansion-history-session-batch.sqlite3')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def validate_paths(root, snapshot):
    if root == snapshot or root in snapshot.parents or snapshot in root.parents:
        raise ValueError('Cache and snapshot must be separate directories')


def backup(root, snapshot):
    root, snapshot = Path(root).resolve(), Path(snapshot).resolve()
    validate_paths(root, snapshot)
    if snapshot.exists() and any(snapshot.iterdir()):
        raise ValueError('Snapshot must be empty')
    required = sum((root / n).stat().st_size for n in FILES if (root / n).exists())
    if shutil.disk_usage(snapshot.parent).free < required + 1024**3:
        raise RuntimeError('Insufficient snapshot headroom: retain at least 1 GiB free')
    snapshot.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest = {'root': str(root), 'files': {}}
    for name in FILES:
        source = root / name
        if source.is_symlink():
            raise ValueError('Cache symlinks are not supported')
        if source.exists():
            if name.endswith('.sqlite3'):
                with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
                    with closing(sqlite3.connect(snapshot / name)) as target:
                        original.backup(target)
                shutil.copystat(source, snapshot / name)
            else:
                shutil.copy2(source, snapshot / name)
            stat = source.stat()
            manifest['files'][name] = {'sha256': digest(snapshot / name),
                                       'uid': stat.st_uid, 'gid': stat.st_gid}
    # Disaster-recovery copy only: restore deliberately never touches Discord.
    alert = root / 'discord-alerts.sqlite3'
    if alert.exists():
        with closing(sqlite3.connect(alert.as_uri() + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(snapshot / alert.name)) as target:
                source.backup(target)
    (snapshot / 'manifest.json').write_text(json.dumps(manifest))


def restore(root, snapshot):
    root, snapshot = Path(root).resolve(), Path(snapshot).resolve()
    validate_paths(root, snapshot)
    manifest = json.loads((snapshot / 'manifest.json').read_text())
    if manifest['root'] != str(root) or not set(manifest['files']).issubset(FILES):
        raise ValueError('Snapshot belongs to another cache or has invalid files')
    # Verify every file before changing anything.
    for name, metadata in manifest['files'].items():
        if (snapshot / name).is_symlink() or digest(snapshot / name) != metadata['sha256']:
            raise ValueError('Snapshot checksum mismatch: ' + name)
    for name in FILES:
        target = root / name
        if target.is_symlink():
            raise ValueError('Cache symlinks are not supported')
    for name in FILES:
        target = root / name
        metadata = manifest['files'].get(name)
        if metadata:
            fd, temporary = tempfile.mkstemp(dir=root, prefix='.rollback-')
            os.close(fd)
            try:
                shutil.copy2(snapshot / name, temporary)
                if os.geteuid() == 0:
                    os.chown(temporary, metadata['uid'], metadata['gid'])
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
        else:
            target.unlink(missing_ok=True)
        if name.endswith('.sqlite3'):
            for suffix in ('-wal', '-shm'):
                (root / (name + suffix)).unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['backup', 'restore'])
    parser.add_argument('root', type=Path)
    parser.add_argument('snapshot', type=Path)
    args = parser.parse_args()
    globals()[args.action](args.root, args.snapshot)
    print(json.dumps({'action': args.action, 'status': 'passed', 'snapshot': str(args.snapshot)}))
