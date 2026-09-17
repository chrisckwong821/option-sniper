"""Restore or rebuild bucket calibration using the same startup cache hierarchy."""
import json
import os
import subprocess
import sys
from pathlib import Path
from .shared_cache import SharedCache


def ensure_bucket_statistics(root, state, allow_download=True, *, ticker_source=None):
    root = Path(root).resolve()
    if not SharedCache(root).enabled:
        return
    target = state['today']['anchor_date']
    stats_path = root / 'iv-expansion-statistics-session-batch.json'
    def valid():
        try:
            data = json.loads(stats_path.read_text())
            return (data.get('last_calibration_session') == target and data.get('months') == 6
                    and any(r.get('method') == 'moneyness-matched-black76-robust-mad-min2dte-v2' for r in data.get('statistics', [])))
        except (OSError, ValueError):
            return False
    if valid():
        return
    from .computed_cache import restore
    restore(root, target, state['screening_policy_fingerprint'], include_state=False)
    if valid():
        return
    if not (root / 'iv-expansion-history-session-batch.sqlite3').exists():
        # An older observation database is a useful incremental reconstruction seed.
        restore(root, target, state['screening_policy_fingerprint'], include_state=False, history_only=True)
    command = [sys.executable, '-m', 'scripts.iv_expansion_history', '--mode', 'batch',
               '--ticker-source', str(ticker_source or root / 'dashboard-state.json'), '--end-date', target,
               '--database', str(root / 'iv-expansion-history-session-batch.sqlite3'),
               '--cache-root', str(root / 'iv-expansion-history'), '--stats-output', str(stats_path),
               '--max-cost', '5', '--download-retries', '0', '--download-workers', '1']
    env = {**os.environ, 'SIGMA_CACHE_ROOT': str(root)}
    # Repeated retries and restarts share one reservation ledger per session.
    # A retry must never silently obtain another full download allowance.
    budget = root / 'reconstruction-runs' / ('bucket-' + target + '.json')
    budget.parent.mkdir(parents=True, exist_ok=True)
    try:
        with budget.open('x') as stream:
            stream.write('{}')
    except FileExistsError:
        pass
    env['SIGMA_RECONSTRUCTION_BUDGET_FILE'] = str(budget)
    env['SIGMA_RECONSTRUCTION_SESSION'] = target
    if not allow_download:
        env['SIGMA_RECONSTRUCTION_MAX_COST'] = '0'
    with (root / 'bucket-reconstruction.log').open('w') as output:
        result = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], env=env,
                                stdout=output, stderr=subprocess.STDOUT, check=False, timeout=900)
    if result.returncode:
        raise RuntimeError('Bucket reconstruction failed; see bucket-reconstruction.log. No further download fallback was attempted.')
    from .computed_cache import publish
    publish(root)
