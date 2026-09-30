"""Dedicated S3-backed incremental bucket worker; never run on a serving host."""
import argparse
import json
import os
import sys
import subprocess
from datetime import UTC, date, datetime, time
from pathlib import Path
from .computed_cache import restore as restore_computed
from .dashboard_state import build_refresh_plan, screening_policy_fingerprint, validate_bundle
from .expected_sigma import EASTERN, ExpectedSigma, sigma_plan
from .iv_reference_refresh import closing_references
from .shared_cache import SharedCache, atomic_json
from .startup_history import ensure_bucket_statistics
from .universe_input import restore as restore_universe
from .weekly_options import load_report, weekly_universe


def calibration_plan(now=None, effective_session=None):
    """Prepare the next session from the latest completed close, even overnight."""
    now = now or datetime.now(UTC)
    completed = None if effective_session else build_refresh_plan(now)
    session = date.fromisoformat(effective_session or completed.next_session)
    activation = datetime.combine(session, time(4), EASTERN)
    plan = sigma_plan(activation)
    if completed and plan['iv_session'] != completed.completed_session:
        raise RuntimeError('Worker plan does not match the latest completed session')
    if datetime.fromisoformat(plan['iv_snapshot_time']) > now:
        raise RuntimeError('Cannot calibrate before the reference session closes')
    return activation, plan


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--calibration-only', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--effective-session', help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.environ['SIGMA_CACHE_ROOT'] = str(root)
    shared = SharedCache(root)
    if not shared.enabled:
        raise RuntimeError('Dedicated worker requires S3')
    activation, plan = calibration_plan(effective_session=args.effective_session)
    fingerprint = screening_policy_fingerprint()
    worker_state_path = root / 'weekly-worker-state.json'
    if args.calibration_only:
        state = validate_bundle(json.loads(worker_state_path.read_text()))
        if state['today']['anchor_date'] != plan['iv_session'] or state.get('screening_policy_fingerprint') != fingerprint:
            raise RuntimeError('Calibration input changed across worker phases')
        ensure_bucket_statistics(root, state, allow_download=True, ticker_source=worker_state_path)
        print(json.dumps({'iv_session': plan['iv_session'], 'status': 'published'}), flush=True)
        return
    # Calibration/history may come from the prior computed release. The current
    # universe is a separate producer input and must match this exact session.
    restore_computed(root, plan['iv_session'], fingerprint,
                     include_state=False, history_only=True)
    state = restore_universe(root, plan['iv_session'], fingerprint)
    report_path = root / 'occ-weekly-options.json'
    subprocess.run([sys.executable, '-m', 'scripts.weekly_options', '--output', str(report_path)], check=True)
    state = weekly_universe(state, load_report(report_path))
    atomic_json(worker_state_path, state)
    print(json.dumps({'status': 'occ_weekly_universe', 'as_of': state['weekly_options']['as_of'],
                      'coverage': state['weekly_options']['coverage']}), flush=True)
    service = ExpectedSigma(root)
    service.refresh(state, effective_at=activation)
    reference_plan, references = closing_references(service, now=activation)
    if reference_plan != plan:
        raise RuntimeError('Closing reference plan changed during calibration')
    eligible = {row['ticker'] for view in ('today', 'snapshot') for row in state[view]['rows']}
    references = {ticker: value for ticker, value in references.items() if ticker in eligible}
    if not references:
        raise RuntimeError('No verified closing inputs for bucket references')
    publication = {'plan': reference_plan, 'references': references}
    ref_path = root / 'bucket-references.json'
    atomic_json(ref_path, publication)
    shared.put('bucket-sigma-min2dte-v2/' + plan['effective_session'] + '.json', ref_path)
    print(json.dumps({'iv_session': plan['iv_session'], 'references': len(references),
                      'status': 'references_published_starting_calibration'}), flush=True)
    # Closing-input fitting can retain a large Python heap. Replace this process
    # before starting calibration so both phases never share the memory budget.
    os.execv(sys.executable, [sys.executable, '-m', 'scripts.history_repair_worker',
                             '--root', str(root), '--calibration-only',
                             '--effective-session', plan['effective_session']])


if __name__ == '__main__':
    main()
