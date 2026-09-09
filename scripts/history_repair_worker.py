"""One bounded repair run, executed outside the live backend container."""
import json
from pathlib import Path
from .startup_history import ensure_bucket_statistics


def main():
    root = Path('/app/.cache/sigma-tape')
    state = json.loads((root / 'dashboard-state.json').read_text())
    ensure_bucket_statistics(root, state, allow_download=True)


if __name__ == '__main__':
    main()
