"""Audit the isolated historical calibration without purchasing data."""
import json
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path

from scripts.iv_expansion_history import history_window, write_statistics


def main():
    root = Path('.cache/sigma-tape')
    payload = json.loads((root / 'iv-expansion-statistics-session-batch.json').read_text())
    connection = sqlite3.connect(root / 'iv-expansion-history-session-batch.sqlite3')
    connection.row_factory = sqlite3.Row
    expected = {str(d) for d in history_window(date(2026, 9, 4), 6).calibration_sessions}
    actual = {r[0] for r in connection.execute('SELECT DISTINCT session_date FROM observations')}
    coverage = [dict(r) for r in connection.execute('''
        SELECT ticker, COUNT(*) AS observations, COUNT(DISTINCT session_date) AS sessions,
               SUM(reference_reliability='low' OR observed_reliability='low') AS low_reliability_marks
        FROM observations GROUP BY ticker ORDER BY ticker
    ''')]
    connection.execute('ATTACH DATABASE ? AS old', (str(root / 'tsem-min2dte-history.sqlite3'),))
    differences = [dict(r) for r in connection.execute('''
        SELECT n.session_date, COUNT(*) AS differing_marks,
               n.expiry AS batch_expiry, o.expiry AS earlier_expiry,
               MAX(ABS(n.iv_log_change_pct-o.iv_log_change_pct)) AS max_log_change_difference
        FROM observations n JOIN old.observations o USING(ticker,session_date,bucket)
        WHERE n.ticker='TSEM' AND (ABS(n.iv_log_change_pct-o.iv_log_change_pct)>1e-8 OR n.expiry<>o.expiry)
        GROUP BY n.session_date ORDER BY n.session_date
    ''')]
    stats = payload['statistics']
    audit = {
        'status': payload['status'],
        'cost': payload['run']['cost'],
        'missing_sessions': sorted(expected-actual),
        'unexpected_sessions': sorted(actual-expected),
        'missing_tickers': sorted(set(payload['tickers'])-{r['ticker'] for r in coverage}),
        'observations': sum(r['observations'] for r in coverage),
        'sessions': len(actual),
        'ticker_count': len(coverage),
        'quick_check': connection.execute('PRAGMA quick_check').fetchone()[0],
        'below_minimum_dte': connection.execute('SELECT COUNT(*) FROM observations WHERE observation_dte_days<2').fetchone()[0],
        'distribution_status_counts': dict(Counter(r['log_iv_change_distribution']['status'] for r in stats)),
        'holdout_score_status_counts': dict(Counter(r['holdout']['score_status'] for r in stats if r['holdout'])),
        'coverage': coverage,
        'tsem_differences': differences,
        'tsem_opening_distributions': [r for r in stats if r['ticker']=='TSEM' and r['bucket']=='09:30'],
        'catalogue_limitation': 'Cycle snapshots omit later listings. Cached TSEM March 31 definitions lack April 10 expiry; April 2 definitions include it. Other observed differences also coincide with added strikes. Batch calibration uses cycle snapshots and is not identical to daily-catalogue calibration.',
    }
    assert not audit['missing_sessions'] and not audit['unexpected_sessions']
    assert not audit['missing_tickers'] and audit['below_minimum_dte']==0
    assert audit['quick_check']=='ok' and audit['cost']['estimated_new_cost_usd']<=83
    write_statistics(root / 'iv-expansion-history-batch-verification.json', audit)
    print(json.dumps({k:v for k,v in audit.items() if k not in ('coverage','tsem_opening_distributions','tsem_differences')}, indent=2))
    print('TSEM opening:', [(r['tenor_class'],r['log_iv_change_distribution']) for r in audit['tsem_opening_distributions']])
    print('TSEM differing dates:',len(differences),'marks:',sum(r['differing_marks'] for r in differences))


if __name__ == '__main__':
    main()
