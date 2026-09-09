"""Fail production promotion promptly if its shared sigma differs from staging."""
from datetime import datetime
import argparse
import json
import math
import time
import urllib.request
from pathlib import Path


def differences(reference, candidate):
    errors = []
    expected = reference.get('expected_sigma', {})
    actual = candidate.get('expected_sigma', {})
    for field in ('version', 'effective_session', 'iv_session', 'iv_snapshot_time', 'horizon_end', 'option_expiry'):
        if not expected.get(field) or expected.get(field) != actual.get(field):
            errors.append('plan.' + field)
    for view in ('today', 'snapshot'):
        left = {r['ticker']: r for r in reference[view]['rows']}
        right = {r['ticker']: r for r in candidate[view]['rows']}
        if left.keys() != right.keys():
            errors.append(view + '.symbols')
        for ticker in left.keys() & right.keys():
            a, b = left[ticker], right[ticker]
            for field in ('sigma_1d_pct', 'annualized_implied_vol_pct', 'return_anchor_price'):
                try:
                    x, y = float(a[field]), float(b[field])
                    equal = math.isfinite(x) and math.isfinite(y) and x > 0 and math.isclose(x, y, rel_tol=1e-10, abs_tol=1e-12)
                except (KeyError, ValueError, TypeError):
                    equal = False
                if not equal:
                    errors.append(f'{view}.{ticker}.{field}')
            for field in ('expected_sigma_status', 'iv_snapshot_time', 'option_expiry', 'return_anchor_date'):
                x, y = a.get(field), b.get(field)
                if field == 'iv_snapshot_time':
                    try:
                        x, y = datetime.fromisoformat(x.replace('Z', '+00:00')), datetime.fromisoformat(y.replace('Z', '+00:00'))
                    except (ValueError, AttributeError):
                        errors.append(f'{view}.{ticker}.{field}')
                        continue
                if x != y:
                    errors.append(f'{view}.{ticker}.{field}')
    return sorted(errors)


def fetch(url):
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--url', required=True)
    parser.add_argument('--capture', action='store_true')
    parser.add_argument('--wait-seconds', type=float, default=30)
    args = parser.parse_args()
    if args.capture:
        state = fetch(args.url)
        assert state.get('expected_sigma', {}).get('iv_session'), 'Staging sigma plan missing'
        assert any(r.get('expected_sigma_status') == 'fresh' for r in state['today']['rows']), 'Staging has no current sigma'
        args.reference.write_text(json.dumps(state))
        print('Captured staging IV session:', state['expected_sigma']['iv_session'])
        return 0
    reference = json.loads(args.reference.read_text())
    deadline = time.monotonic() + args.wait_seconds
    while True:
        try:
            candidate = fetch(args.url)
            loaded = any(r.get('expected_sigma_status') == 'fresh' for r in candidate['today']['rows'])
            if loaded:
                errors = differences(reference, candidate)
                print(json.dumps({'sigma_match': not errors, 'iv_session': candidate.get('expected_sigma', {}).get('iv_session'), 'differences': errors}))
                return int(bool(errors))
        except (OSError, ValueError, KeyError):
            pass  # Allow only bounded startup/cache-restoration time.
        if time.monotonic() >= deadline:
            print('Production sigma did not become ready within the bounded startup window')
            return 1
        time.sleep(1)


if __name__ == '__main__':
    raise SystemExit(main())
