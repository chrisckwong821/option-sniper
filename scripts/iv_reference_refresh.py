"""Repair expansion baselines from validated shared closing inputs, without downloads."""
from datetime import datetime, timedelta
import math
import pandas as pd
from .expected_sigma import read_json, sigma_plan, validated_rows, YEAR_SECONDS
from .nasdaq_universe_movers import prepare_option_pairs, estimate_option_iv


def closing_references(service, now=None):
    plan = sigma_plan(now)
    saved = read_json(service.root / (plan['effective_session'] + '.json'))
    valid = validated_rows(saved, plan)
    inputs = read_json(service.root / ('close-' + plan['iv_session'] + '.json'))
    if not valid or not inputs.get('quotes'):
        return plan, {}
    if inputs.get('source') == 'live_replay' and not inputs.get('replay_verified'):
        return plan, {}
    candidates = [{'ticker': t, 'anchor_price': float(p)} for t, p in inputs.get('spots', {}).items()
                  if t in valid and math.isfinite(float(p)) and float(p) > 0]
    close = datetime.fromisoformat(plan['iv_snapshot_time'])
    frame = pd.DataFrame(inputs['quotes'])
    stamps = pd.to_datetime(frame['ts_recv'], utc=True)
    frame = frame[(stamps >= close - timedelta(minutes=1)) & (stamps <= close)]
    pairs, _ = prepare_option_pairs(frame, candidates, datetime.fromisoformat(plan['option_expiry']).date())
    years = (datetime.fromisoformat(plan['expiry_close']) - close).total_seconds() / YEAR_SECONDS
    references = {}
    for row in candidates:
        estimate = estimate_option_iv(row, pairs.get(row['ticker'], []), years)
        if not estimate:
            continue
        references[row['ticker']] = {
            'reference_iv_pct': estimate['annualized_implied_vol_pct'],
            'iv_reliability': estimate['iv_reliability'],
            'reference_forward': estimate['forward_price'],
            'reference_strikes': estimate['iv_input_strike_values'],
            'moneyness_targets': estimate['iv_input_log_moneyness'],
            'option_expiry': plan['option_expiry'],
            'requested_session': plan['iv_session'], 'reference_session': plan['iv_session'],
            'reference_status': 'yesterday', 'fallback_sessions': 0,
        }
    return plan, references


def activate_references(store, plan, references):
    """Run on the event loop; invalidate old observations before recalculating."""
    tracker = store.iv_tracker
    if tracker.option_expiry_close != datetime.fromisoformat(plan['expiry_close']):
        return 0
    changed = 0
    for ticker, ref in references.items():
        old = tracker.references.get(ticker)
        if not old or (old.get('reference_session') == plan['iv_session']
                       and old.get('reference_status') == 'yesterday'):
            continue
        tracker.references[ticker] = {**old, **ref, 'baseline_revision': old.get('baseline_revision', 1) + 1}
        tracker.results.pop(ticker, None)
        tracker.next_calculation.pop(ticker, None)
        point = tracker.spots.get(ticker)
        if point:
            tracker._schedule_calculation(ticker, datetime.now().astimezone())
        store.dirty_symbols.add(ticker)
        changed += 1
    if changed:
        tracker.cache_dirty = True
        store.invalidate_snapshot()
    return changed
