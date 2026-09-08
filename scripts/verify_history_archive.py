"""Verify all archived files against S3's full-object checksums, then publish the index."""
import argparse
import base64
import gzip
import hashlib
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from awscrt.checksums import crc64nvme
from .shared_cache import SharedCache


def verify(root, *, publish=False):
    root=Path(root)
    manifest=json.loads((root/'history-manifest.json').read_text())
    shared=SharedCache(root)
    if not shared.enabled:
        raise RuntimeError('S3 cache is not configured')
    sizes={}
    for page in shared.client.get_paginator('list_objects_v2').paginate(Bucket=shared.bucket,Prefix=shared.key('history/')):
        sizes.update({row['Key']:row['Size'] for row in page.get('Contents',[])})
    missing=[row['path'] for row in manifest['files'] if sizes.get(shared.key('history/'+row['path'])) != row['bytes']]
    if missing:
        print(json.dumps({'status':'upload_incomplete','remaining_files':len(missing),'total_files':len(manifest['files'])}),flush=True)
        return False
    def check(row):
        crc=0; sha=hashlib.sha256()
        with (root/row['path']).open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):
                crc=crc64nvme(block,crc);sha.update(block)
        if sha.hexdigest()!=row['sha256']:
            raise RuntimeError('Local file changed since inventory: '+row['path'])
        head=shared.client.head_object(Bucket=shared.bucket,Key=shared.key('history/'+row['path']),ChecksumMode='ENABLED')
        expected=base64.b64encode(crc.to_bytes(8,'big')).decode()
        if head.get('ChecksumType')!='FULL_OBJECT' or head.get('ChecksumCRC64NVME')!=expected:
            raise RuntimeError('S3 full-object checksum mismatch or unavailable: '+row['path'])
    with ThreadPoolExecutor(max_workers=8) as pool:
        for _ in pool.map(check,manifest['files']):
            pass
    if publish:
        source=root/'history-manifest.json';compressed=root/'history-manifest.json.gz'
        with source.open('rb') as src,gzip.open(compressed,'wb') as dst:
            shutil.copyfileobj(src,dst)
        shared.put('history/manifest.json.gz',compressed)
    print(json.dumps({'status':'verified','files':len(manifest['files']),
                      'bytes':sum(r['bytes'] for r in manifest['files']),
                      'invalid_dbn_files':sum(bool(r.get('invalid')) for r in manifest['files']),
                      'index_published':publish}),flush=True)
    return True


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('.cache/sigma-tape'))
    parser.add_argument('--publish',action='store_true')
    args=parser.parse_args()
    raise SystemExit(0 if verify(args.root,publish=args.publish) else 2)
