"""Private S3 cache transport. Only an explicit 404 counts as a miss."""
from __future__ import annotations
import hashlib
import json
import os
import tempfile
from pathlib import Path


class SharedCacheError(RuntimeError):
    pass


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(json.dumps(payload, default=str).encode())
    os.replace(temporary, path)


class SharedCache:
    def __init__(self, root, client=None, bucket=None, prefix=None):
        self.root = Path(root).resolve()
        config_path = self.root / 'shared-cache-config.json'
        self.config = json.loads(config_path.read_text()) if config_path.exists() else {}
        self.bucket = bucket or os.environ.get('SIGMA_S3_BUCKET') or self.config.get('bucket')
        self.prefix = (prefix or os.environ.get('SIGMA_S3_PREFIX') or 'option-sniper/cache/v1').strip('/')
        self._client = client

    @property
    def enabled(self):
        return bool(self.bucket)

    @property
    def client(self):
        if self._client is None:
            import boto3
            from botocore.config import Config
            region = os.environ.get('SIGMA_S3_REGION') or self.config.get('region', 'ap-southeast-1')
            # Login credential providers also need the region on the Session.
            session = boto3.Session(profile_name=os.environ.get('SIGMA_AWS_PROFILE') or self.config.get('profile') or None, region_name=region)
            self._client = session.client('s3', region_name=region,
                                          config=Config(connect_timeout=10, read_timeout=60, retries={'max_attempts': 2}))
        return self._client

    def key(self, suffix):
        if '..' in Path(suffix).parts or str(suffix).startswith('/'):
            raise ValueError('Unsafe cache key')
        return f'{self.prefix}/{suffix}'

    def get(self, suffix, destination, sha256=None):
        if not self.enabled:
            return False
        from botocore.exceptions import ClientError
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.key(suffix))
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
                temporary = Path(stream.name)
                with response['Body'] as body:
                    for block in iter(lambda: body.read(1024 * 1024), b''):
                        stream.write(block)
            sha256 = sha256 or response.get('Metadata', {}).get('sha256')
            if sha256 and digest(temporary) != sha256:
                raise SharedCacheError(f'Checksum mismatch: {suffix}')
            os.replace(temporary, destination)
            return True
        except ClientError as exc:
            if exc.response.get('Error', {}).get('Code') in ('NoSuchKey', '404', 'NotFound'):
                return False
            raise SharedCacheError(f'S3 access failed for {suffix}: {exc.response["Error"]["Code"]}') from exc
        except SharedCacheError:
            raise
        except Exception as exc:
            raise SharedCacheError(f'S3 request failed for {suffix}: {type(exc).__name__}') from exc
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def list_keys(self, suffix):
        if not self.enabled:
            return []
        try:
            pages = self.client.get_paginator('list_objects_v2').paginate(
                Bucket=self.bucket, Prefix=self.key(suffix))
            return [item['Key'][len(self.prefix) + 1:] for page in pages for item in page.get('Contents', [])]
        except Exception as exc:
            raise SharedCacheError(f'S3 index listing failed: {type(exc).__name__}') from exc

    def put(self, suffix, source):
        if self.enabled:
            try:
                self.client.upload_file(str(source), self.bucket, self.key(suffix),
                                        ExtraArgs={'ServerSideEncryption': 'AES256', 'Metadata': {'sha256': digest(source)}})
            except Exception as exc:
                raise SharedCacheError(f'S3 upload failed for {suffix}: {type(exc).__name__}') from exc

    def update_json(self, suffix, update):
        """Conditionally publish a merged document, retrying competing writers."""
        if not self.enabled:
            return update({})
        from botocore.exceptions import ClientError
        for _ in range(4):
            try:
                try:
                    response = self.client.get_object(Bucket=self.bucket, Key=self.key(suffix))
                    with response["Body"] as body:
                        raw = body.read()
                    checksum = response.get("Metadata", {}).get("sha256")
                    if checksum and hashlib.sha256(raw).hexdigest() != checksum:
                        raise SharedCacheError("Shared JSON checksum mismatch")
                    previous = json.loads(raw)
                    condition = {"IfMatch": response["ETag"]}
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") not in ("NoSuchKey", "404", "NotFound"):
                        raise
                    previous, condition = {}, {"IfNoneMatch": "*"}
                value = update(previous)
                if value == previous:
                    return value
                raw = json.dumps(value, sort_keys=True).encode()
                self.client.put_object(Bucket=self.bucket, Key=self.key(suffix), Body=raw,
                    ServerSideEncryption="AES256", Metadata={"sha256": hashlib.sha256(raw).hexdigest()},
                    ContentType="application/json", **condition)
                return value
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in ("PreconditionFailed", "412", "ConditionalRequestConflict", "409"):
                    continue
                raise SharedCacheError("Shared JSON update failed: " + exc.response["Error"]["Code"]) from exc
            except SharedCacheError:
                raise
            except Exception as exc:
                raise SharedCacheError("Shared JSON update failed: " + type(exc).__name__) from exc
        raise SharedCacheError("Shared JSON update conflicted repeatedly")

    def restore_file(self, path):
        path = Path(path).resolve()
        if path.exists():
            return True
        try:
            relative = path.relative_to(self.root).as_posix()
        except ValueError:
            return False
        checksum = None
        for manifest_path in (self.root / 'history-manifest.json', self.root / 'remote-history-manifest.json'):
            if manifest_path.exists():
                entries = json.loads(manifest_path.read_text()).get('files', [])
                entry = next((e for e in entries if e['path'] == relative), None)
                if entry:
                    if entry.get('invalid'):
                        raise SharedCacheError('Archived file is undecodable: ' + relative)
                    checksum = entry.get('sha256')
                    break
        return self.get('history/' + relative, path, checksum)
