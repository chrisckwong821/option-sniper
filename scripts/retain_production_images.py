"""Remove only obsolete sigma-tape tags; retain explicit releases and container images."""
import argparse
import json
import subprocess
from pathlib import Path


def obsolete_tags(tags, keep, container_refs):
    protected = set(keep) | set(container_refs)
    return sorted(tag for tag in tags if tag.startswith('sigma-tape:')
                  and tag != 'sigma-tape:<none>' and tag not in protected)


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--keep', action='append', default=[], help='Release tag to retain')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--host', action='store_true', help='Read active and rollback releases from host state')
    args = parser.parse_args()
    if args.host:
        base = Path('/opt/sigma-tape')
        args.keep += [(base / 'current').resolve(strict=True).name,
                      (base / 'rollback-image').read_text().strip()]
    if not args.keep or any(not v or not all(c.isalnum() or c in '-_' for c in v) for v in args.keep):
        parser.error('Valid retained release tags are required')
    keep = ['sigma-tape:' + version for version in args.keep]
    for tag in keep:
        docker('image', 'inspect', tag)  # Fail closed if a required recovery image is absent.
    ids = docker('ps', '-aq').splitlines()
    containers = json.loads(docker('inspect', *ids)) if ids else []
    refs = [c['Config']['Image'] for c in containers]
    tags = docker('image', 'ls', '--format', '{{.Repository}}:{{.Tag}}').splitlines()
    for tag in obsolete_tags(tags, keep, refs):
        print(json.dumps({'action': 'remove' if args.apply else 'would_remove', 'tag': tag}), flush=True)
        if args.apply:
            # No force: a concurrent container creation must prevent deletion.
            subprocess.run(['docker', 'image', 'rm', tag], check=True)


if __name__ == '__main__':
    main()
