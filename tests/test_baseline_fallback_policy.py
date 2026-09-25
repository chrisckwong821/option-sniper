import contextlib
import io
import json
import os
import sqlite3
import unittest
from datetime import date
from unittest.mock import Mock, patch

import pandas as pd
from scripts import iv_expansion_history as h
from scripts.reconstruction_budget import HistoricalLookbackBlocked, reserve


class BaselineFallbackPolicyTests(unittest.TestCase):
    def build(self, failure=None, blocked=('VOD',), fallback_failure=None):
        observation = date(2026, 9, 24)
        prior = date(2026, 9, 23)
        expiry = date(2026, 10, 2)
        rows = [{'ticker': t} for t in ('GOOD', 'VOD', 'CACHED')]

        def contracts(client, ledger, candidates, session, **kwargs):
            if failure is not None:
                raise failure
            if session < prior and fallback_failure is not None:
                raise fallback_failure
            if session < prior and any(r['ticker'] in blocked for r in candidates):
                raise HistoricalLookbackBlocked('blocked old download')
            return {r['ticker']: {expiry: []} for r in candidates}

        def estimate(ticker, spot, pairs, expiry, close_mark):
            if ticker != 'GOOD' and close_mark.date() == prior:
                return None
            return {'annualized_implied_vol_pct': 25, 'iv_input_log_moneyness': [0]}

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(h, '_contracts_for_session', side_effect=contracts))
            stack.enter_context(patch.object(h, '_spot_frame', return_value=pd.DataFrame()))
            stack.enter_context(patch.object(h, 'latest_spots', side_effect=lambda frame, tickers, mark: {t: 100 for t in tickers}))
            stack.enter_context(patch.object(h, 'select_baseline_symbols', side_effect=lambda catalog, spots: ([], {t: [] for t in spots})))
            stack.enter_context(patch.object(h, '_option_frames', return_value=pd.DataFrame()))
            stack.enter_context(patch.object(h, 'frame_window', return_value=pd.DataFrame()))
            stack.enter_context(patch.object(h, 'prepare_snapshot_pairs', return_value={}))
            stack.enter_context(patch.object(h, 'estimate_snapshot_from_pairs', side_effect=estimate))
            return h.build_baselines(Mock(), Mock(), Mock(), rows, observation,
                                     [prior, date(2026, 9, 22)],
                                     max_fallback_sessions=1, option_symbol_chunk_size=1500)

    def test_blocked_optional_ticker_keeps_valid_and_cached_peer_results(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.build()
        self.assertEqual(set(result), {'GOOD', 'CACHED'})
        self.assertEqual(result['CACHED'].reference_session, date(2026, 9, 22))
        gaps = [json.loads(line)['baseline_gap'] for line in output.getvalue().splitlines() if line.startswith('{')]
        self.assertEqual(gaps[0]['tickers'], ['VOD'])
        self.assertEqual(gaps[0]['reason'], 'historical_lookback_blocked')

    def test_required_reference_failure_stays_fatal(self):
        with self.assertRaises(HistoricalLookbackBlocked):
            self.build(HistoricalLookbackBlocked('required data'))

    def test_unexpected_auth_budget_and_corruption_errors_stay_fatal(self):
        for error in (RuntimeError('budget exhausted'), ValueError('corrupt cache'), PermissionError('authentication')):
            with self.subTest(error=error), self.assertRaises(type(error)):
                self.build(error)
            with self.subTest(fallback_error=error), self.assertRaises(type(error)):
                self.build(fallback_failure=error)

    def test_cache_available_old_fallback_is_usable(self):
        self.assertEqual(set(self.build(blocked=())), {'GOOD', 'VOD', 'CACHED'})

    def test_zero_cost_does_not_bypass_historical_age_guard(self):
        with patch.dict(os.environ, {'SIGMA_RECONSTRUCTION_SESSION': '2026-09-24'}):
            with self.assertRaises(HistoricalLookbackBlocked):
                reserve(0, {'start': '2026-09-16', 'end': '2026-09-17'})

    def test_target_coverage_uses_actual_session_and_records_missing_tickers(self):
        with contextlib.closing(sqlite3.connect(':memory:')) as db:
            db.execute('CREATE TABLE observations (ticker TEXT, session_date TEXT)')
            tickers = [f'T{i}' for i in range(10)]
            db.executemany('INSERT INTO observations VALUES (?, ?)', [(t, '2026-09-23') for t in tickers])
            with self.assertRaisesRegex(RuntimeError, 'below 90%'):
                h.calibration_coverage(db, tickers, date(2026, 9, 24))
            db.executemany('INSERT INTO observations VALUES (?, ?)', [(t, '2026-09-24') for t in tickers[:9]])
            result = h.calibration_coverage(db, tickers, date(2026, 9, 24))
            self.assertEqual(result['observed_tickers'], 9)
            self.assertEqual(result['missing_tickers'], ['T9'])
