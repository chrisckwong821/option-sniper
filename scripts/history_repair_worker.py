"""Dedicated S3-backed incremental bucket worker; never run on a serving host."""
import argparse
import json
import os
from pathlib import Path
from .computed_cache import restore as restore_computed
from .dashboard_state import screening_policy_fingerprint
from .expected_sigma import ExpectedSigma, sigma_plan
from .iv_reference_refresh import closing_references
from .shared_cache import SharedCache, atomic_json
from .startup_history import ensure_bucket_statistics
from .universe_input import restore as restore_universe


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.environ['SIGMA_CACHE_ROOT'] = str(root)
    os.environ['SIGMA_BUCKET_CACHED_CONTRACTS_ONLY'] = '1'
    shared = SharedCache(root)
    if not shared.enabled:
        raise RuntimeError('Dedicated worker requires S3')
    plan = sigma_plan()
    fingerprint = screening_policy_fingerprint()
    # Calibration/history may come from the prior computed release. The current
    # universe is a separate producer input and must match this exact session.
    restore_computed(root, plan['iv_session'], fingerprint,
                     include_state=False, history_only=True)
    state = restore_universe(root, plan['iv_session'], fingerprint)
    service = ExpectedSigma(root)
    service.refresh(state)
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
