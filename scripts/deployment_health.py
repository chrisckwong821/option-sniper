"""Semantic release readiness checks, beyond an HTTP 200 response."""
import argparse
from datetime import datetime, timezone
import json
import sys
import urllib.request
from zoneinfo import ZoneInfo
import exchange_calendars


def check(payload, now=None):
    now = now or datetime.now(timezone.utc)
    errors = []
    def fresh(value, seconds):
        try:
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return -10 <= (now - stamp).total_seconds() <= seconds
        except (TypeError, ValueError, AttributeError):
            return False
    if not fresh(payload.get('server_time'), 60):
        errors.append('Health response timestamp is missing or stale')
    state = payload.get('dashboard_state', {})
    calibration = payload.get('calibration', {})
    if state.get('status') != 'fresh':
        errors.append('Dashboard cache is not fresh')
    iv = payload.get('iv_expansion', {})
    reference_date = iv.get('reference_date') or state.get('data_date')
    if not calibration.get('active_version') or calibration.get('active_date') != reference_date:
        errors.append('Compatible sigma calibration is not loaded for the dashboard date')
    if calibration.get('error'):
        errors.append('Calibration refresh reports an error')
    if not iv.get('reference_symbols') or not iv.get('calculated_symbols'):
        errors.append('IV references or observations are missing')
    feeds = payload.get('feeds', {})
    for name in ('EQUS.MINI', 'OPRA.PILLAR'):
        if feeds.get(name, {}).get('state') in ('offline', 'degraded'):
            errors.append(name + ' is offline or degraded')
    # Use the real calendar, including holidays and shortened sessions, rather
    # than trusting the backend's weekday-only session label.
    calendar = exchange_calendars.get_calendar('XNYS')
    session = now.astimezone(ZoneInfo('America/New_York')).date().isoformat()
    regular = calendar.is_session(session) and calendar.session_open(session) <= now < calendar.session_close(session)
    if regular:
        for name in ('EQUS.MINI', 'OPRA.PILLAR'):
            if not fresh(feeds.get(name, {}).get('last_record_at'), 180):
                errors.append(name + ' has no fresh records during regular trading')
        points = payload.get('iv_expansions', [])
        if not any(fresh(p.get('last_valid_at'), 180) for p in points):
            errors.append('No fresh calculated IV observation during regular trading')
    return errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8765/api/health')
    args = parser.parse_args()
    try:
        with urllib.request.urlopen(args.url, timeout=10) as response:
            payload = json.load(response)
        errors = check(payload)
    except Exception as exc:
        errors = [f'Health request failed: {type(exc).__name__}']
    print(json.dumps({'ready': not errors, 'errors': errors}))
    return bool(errors)


if __name__ == '__main__':
    sys.exit(main())
