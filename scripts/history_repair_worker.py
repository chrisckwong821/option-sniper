"""Dedicated S3-backed incremental bucket worker; never run on a serving host."""
import argparse
import json
import os
from pathlib import Path
from .computed_cache import restore
from .dashboard_state import screening_policy_fingerprint
from .expected_sigma import ExpectedSigma, sigma_plan
from .iv_reference_refresh import closing_references
from .shared_cache import SharedCache, atomic_json
from .startup_history import ensure_bucket_statistics


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.environ['SIGMA_CACHE_ROOT'] = str(root)
    shared = SharedCache(root)
    if not shared.enabled:
        raise RuntimeError('Dedicated worker requires S3')
    plan = sigma_plan()
    state_path = root / 'dashboard-state.json'
    restore(root, plan['iv_session'], screening_policy_fingerprint())
    state = json.loads(state_path.read_text())
    if state['today']['anchor_date'] != plan['iv_session']:
        raise RuntimeError('Shared universe does not cover required completed session')
    service = ExpectedSigma(root)
    service.sync_shared(plan)
    reference_plan, references = closing_references(service)
    if not references:
        raise RuntimeError('No verified closing inputs for bucket references')
    publication = {'plan': reference_plan, 'references': references}
    ref_path = root / 'bucket-references.json'
    atomic_json(ref_path, publication)
    shared.put('bucket-sigma/' + plan['effective_session'] + '.json', ref_path)
    ensure_bucket_statistics(root, state, allow_download=True)
    print(json.dumps({'iv_session': plan['iv_session'], 'references': len(references), 'status': 'published'}), flush=True)


if __name__ == '__main__':
    main()
