"""Join the latest completed-session movers to calibrated IV buckets."""
import json
import sqlite3
from pathlib import Path

from scripts.iv_expansion_history import write_statistics


def main():
    root = Path('.cache/sigma-tape')
    captured = json.loads((root / 'live-movers-sigma-input.json').read_text())
    calibration = json.loads((root / 'iv-expansion-statistics-session-batch.json').read_text())
    statistics = calibration['statistics']
    session = calibration['holdout_session']
    bucket = '15:30'
    source = captured['state']['today']
    assert source['data_date'] == session, 'Mover date must match the historical observation date'
    index = {(r['ticker'], r['bucket'], r['tenor_class']): r for r in statistics}
    connection = sqlite3.connect(root / 'iv-expansion-history-session-batch.sqlite3')
    connection.row_factory = sqlite3.Row
    marks = {r['ticker']: dict(r) for r in connection.execute(
        'SELECT * FROM observations WHERE session_date=? AND bucket=?', (session, bucket)
    )}
    movers = []
    for row in source['rows']:
        price_z = row.get('prior_session_z_score')
        if price_z is None or abs(price_z) <= 1:
            continue
        mark = marks.get(row['ticker'])
        result = {'ticker': row['ticker'], 'price_z': price_z,
                  'gain_pct': row.get('prior_session_gain_pct'), 'status': 'missing_iv'}
        if mark:
            stat = index[(row['ticker'], bucket, mark['observation_tenor_class'])]
            distribution = stat['log_iv_change_distribution']
            holdout = stat['holdout']
            result.update({
                'iv_expansion_pct': mark['iv_expansion_pct'],
                'bucket_sigma_log_points': distribution.get('robust_sigma'),
                'bucket_median_log_points': distribution.get('median'),
                'iv_expansion_z': holdout['iv_expansion_z'],
                'status': holdout['score_status'], 'sample_count': distribution['n'],
                'tenor_class': mark['observation_tenor_class'], 'expiry': mark['expiry'],
                'reference_session': mark['reference_session'],
                'observation_timestamp': mark['observation_ts'],
            })
        movers.append(result)
    movers.sort(key=lambda row: abs(row['price_z']), reverse=True)
    report = {
        'requested_live_date': captured['live']['trading_date'],
        'observation_date': session, 'is_current_live': False,
        'bucket': bucket, 'mark_time_et': '15:59',
        'selection': 'Latest completed session, absolute price z-score > 1',
        'reason': 'Local feed snapshot has only September 4 prices and persisted IVs; no September 8 observations.',
        'sigma_units': 'percentage points of 100*ln(observed_IV/reference_IV)',
        'scoring': '(100*ln(observed_IV/reference_IV) - bucket_median) / bucket_robust_sigma',
        'history_excludes_observation_date': True, 'movers': movers,
    }
    write_statistics(root / 'live-movers-iv-bucket-sigma-report.json', report)
    write_statistics(root / 'all-ticker-iv-bucket-sigmas.json', {
        'tickers': calibration['tickers'], 'statistics': statistics,
        'holdout_session': session, 'method': statistics[0]['method'],
    })
    print('Mover count:', len(movers))
    for row in movers[:12]:
        print(json.dumps(row))


if __name__ == '__main__':
    main()
