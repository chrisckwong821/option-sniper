"""Repair expansion baselines from validated shared closing inputs, without downloads."""
from datetime import datetime
import math
import pandas as pd
from .expected_sigma import (FALLBACK_CLOSE_WINDOW_MINUTES, YEAR_SECONDS,
                             latest_closing_estimates, read_json, sigma_plan,
                             validated_rows)


def align_serving_expiry(bundle, plan, references):
    """Adapt cached screening rows to the active expiry without fitting IV."""
    import copy
    result = copy.deepcopy(bundle)
    result['refresh_plan']['option_expiry'] = plan['option_expiry']
    result['refresh_plan']['option_expiry_close'] = plan['expiry_close']
    for view in ('today', 'snapshot'):
        result[view]['option_expiry'] = plan['option_expiry']
        for row in result[view]['rows']:
            row['option_expiry'] = plan['option_expiry']
            ref = references.get(row['ticker'])
            old = row.get('iv_expansion_reference') or {}
            if ref:
                row['iv_expansion_reference'] = {
                    'method': 'moneyness-matched-black76-v1',
                    'annualized_implied_vol_pct': ref['reference_iv_pct'],
                    'forward_price': ref['reference_forward'],
                    'strikes': ref['reference_strikes'],
                    'log_moneyness': ref['moneyness_targets'],
                    **ref,
                }
                row['iv_chain'] = {**(row.get('iv_chain') or {}),
                                   'strikes': ref['reference_strikes']}
            elif old.get('option_expiry') != plan['option_expiry']:
                row['iv_expansion_reference'] = {}
                row['iv_chain'] = {**(row.get('iv_chain') or {}), 'strikes': []}
    return result


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
    years = (datetime.fromisoformat(plan['expiry_close']) - close).total_seconds() / YEAR_SECONDS
    estimates, quote_times = latest_closing_estimates(
        frame, candidates, datetime.fromisoformat(plan['option_expiry']).date(), close,
        FALLBACK_CLOSE_WINDOW_MINUTES, years,
    )
    references = {}
    for row in candidates:
        estimate = estimates.get(row['ticker'])
        if not estimate:
            continue
        references[row['ticker']] = {
            'reference_iv_pct': estimate['annualized_implied_vol_pct'],
            'iv_reliability': estimate['iv_reliability'],
            'reference_forward': estimate['forward_price'],
            'reference_strikes': estimate['iv_input_strike_values'],
            'moneyness_targets': estimate['iv_input_log_moneyness'],
            'iv_quote_time': quote_times.get(row['ticker']),
            'iv_quote_age_minutes': (
                close - datetime.fromisoformat(quote_times[row['ticker']])
            ).total_seconds() / 60,
            'option_expiry': plan['option_expiry'],
            'requested_session': plan['iv_session'], 'reference_session': plan['iv_session'],
            'reference_status': 'yesterday', 'fallback_sessions': 0,
        }
    return plan, references


def shared_references(service, now=None):
    """Serving instances only download validated worker output; no IV fitting."""
    import tempfile
    plan = sigma_plan(now)
    with tempfile.TemporaryDirectory() as folder:
        path = __import__('pathlib').Path(folder) / 'references.json'
        if not service.shared.get('bucket-sigma-min2dte-v2/' + plan['effective_session'] + '.json', path):
            return plan, {}
        payload = read_json(path)
    if payload.get('plan') != plan:
        raise ValueError('Bucket reference publication has incorrect session')
    refs = payload.get('references', {})
    if not isinstance(refs, dict) or not refs:
        raise ValueError('Empty bucket reference publication')
    for ref in refs.values():
        if (ref.get('reference_session') != plan['iv_session'] or ref.get('requested_session') != plan['iv_session']
                or ref.get('reference_status') != 'yesterday' or ref.get('option_expiry') != plan['option_expiry']):
            raise ValueError('Bucket reference provenance mismatch')
        for key in ('reference_iv_pct', 'reference_forward'):
            if not math.isfinite(float(ref[key])) or float(ref[key]) <= 0:
                raise ValueError('Invalid bucket reference value')
        targets, strikes = ref.get('moneyness_targets'), ref.get('reference_strikes')
        if not targets or not strikes or len(targets) != len(strikes) or not all(math.isfinite(float(v)) for v in targets + strikes):
            raise ValueError('Invalid bucket reference strikes')
    return plan, refs


def activate_references(store, plan, references):
    """Run on the event loop; invalidate old observations before recalculating."""
    tracker = store.iv_tracker
    if tracker.option_expiry_close != datetime.fromisoformat(plan['expiry_close']):
        return 0
    changed = 0
    for ticker, ref in references.items():
        old = tracker.references.get(ticker)
        if old and (old.get('reference_session') == plan['iv_session']
                       and old.get('reference_status') == 'yesterday'):
            continue
        if not old:
            root = getattr(tracker, 'pending_roots', {}).get(ticker)
            if not root:
                continue
            old = {'occ_root': root}
            from .nasdaq_universe_movers import normalize_symbol
            tracker.ticker_by_root[normalize_symbol(root)] = ticker
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
