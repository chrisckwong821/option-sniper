import math
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.iv_expansion import (
    IvExpansionTracker,
    format_occ_symbol,
    iv_expansion_level,
    option_contract_symbols,
)
from scripts.nasdaq_universe_movers import (
    RISK_FREE_RATE,
    black76_straddle,
    estimate_option_iv,
)


class DeferredCalculationTests(unittest.TestCase):
    def test_coalesces_quotes_and_flushes_trailing_update_after_one_minute(self):
        with tempfile.TemporaryDirectory() as folder:
            tracker = IvExpansionTracker([], '2026-09-11T20:00:00+00:00',
                                         '2026-09-04', Path(folder) / 'iv.json',
                                         calculation_interval_seconds=60)
            tracker.references['AAA'] = {'option_expiry': '2026-09-11'}
            tracker.ticker_by_root['AAA'] = 'AAA'
            tracker._recalculate = Mock(return_value=True)
            symbol = format_occ_symbol('AAA', '2026-09-11', 'C', 100)
            first = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
            last = first.replace(second=30)
            for stamp, bid in [(first, 1), (last, 2)]:
                self.assertIsNone(tracker.update_option_quote(symbol, bid, bid + .1, stamp))
            tracker._recalculate.assert_not_called()
            self.assertEqual(tracker.quotes['AAA'][(100.0, 'C')]['bid'], 2)
            with patch('scripts.iv_expansion.monotonic', return_value=100):
                self.assertEqual(tracker.flush_calculations(), ['AAA'])
            tracker._recalculate.assert_called_once_with('AAA', last)
            trailing = last.replace(second=45)
            tracker.update_option_quote(symbol, 3, 3.1, trailing)
            with patch('scripts.iv_expansion.monotonic', return_value=159):
                self.assertEqual(tracker.flush_calculations(), [])
            with patch('scripts.iv_expansion.monotonic', return_value=160):
                self.assertEqual(tracker.flush_calculations(), ['AAA'])
            tracker._recalculate.assert_called_with('AAA', trailing)
            self.assertEqual(tracker._recalculate.call_count, 2)

    def test_batches_are_bounded_and_fair(self):
        with tempfile.TemporaryDirectory() as folder:
            tracker = IvExpansionTracker([], '2026-09-11T20:00:00+00:00',
                                         '2026-09-04', Path(folder) / 'iv.json',
                                         calculation_interval_seconds=60)
            tracker._recalculate = Mock(return_value=True)
            stamp = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
            for ticker in ['AAA', 'BBB', 'CCC']:
                tracker._schedule_calculation(ticker, stamp)
            with patch('scripts.iv_expansion.monotonic', return_value=100):
                self.assertEqual(tracker.flush_calculations(limit=1), ['AAA'])
                tracker._schedule_calculation('AAA', stamp)
                self.assertEqual(tracker.flush_calculations(limit=1), ['BBB'])
                self.assertEqual(tracker.flush_calculations(limit=1), ['CCC'])


def option_pair(forward, strike, annual_iv, year_fraction, root="AAA", expiry="2026-08-28"):
    discount = math.exp(-RISK_FREE_RATE * year_fraction)
    total_vol = annual_iv * math.sqrt(year_fraction)
    straddle = black76_straddle(forward, strike, total_vol, discount)
    parity = discount * (forward - strike)
    call_mid = 0.5 * (straddle + parity)
    put_mid = 0.5 * (straddle - parity)

    def quote(mid, side):
        spread = min(0.01, mid * 0.2)
        return {
            "bid": mid - spread / 2,
            "ask": mid + spread / 2,
            "mid": mid,
            "spread": spread,
            "raw_symbol": format_occ_symbol(root, expiry, side, strike),
        }

    return {
        "strike": float(strike),
        "call": quote(call_mid, "C"),
        "put": quote(put_mid, "P"),
    }


def reference_row(reference):
    return {
        "ticker": "AAA",
        "option_expiry": "2026-08-28",
        "iv_expansion_reference": {
            "method": "moneyness-matched-black76-v1",
            "annualized_implied_vol_pct": reference["annualized_implied_vol_pct"],
            "forward_price": reference["forward_price"],
            "strikes": reference["iv_input_strike_values"],
            "log_moneyness": reference["iv_input_log_moneyness"],
            "option_expiry": "2026-08-28",
            "observation_time": "2026-08-26T16:00:00-04:00",
            "requested_session": "2026-08-27",
            "reference_session": "2026-08-26",
            "reference_status": "yesterday",
            "fallback_sessions": 0,
            "baseline_revision": 2,
        },
        "iv_chain": {
            "occ_root": "AAA",
            "strikes": [95, 97.5, 100, 102.5, 105, 107.5, 110, 112.5, 115],
        },
    }


class MoneynessMatchingTests(unittest.TestCase):
    def test_live_iv_uses_shifted_strikes_for_same_moneyness_points(self):
        reference_years = 7 / 365
        reference = estimate_option_iv(
            {"ticker": "AAA", "anchor_price": 100},
            [
                option_pair(100, strike, 0.50, reference_years)
                for strike in (95, 97.5, 100, 102.5, 105)
            ],
            reference_years,
        )
        live_years = 6 / 365
        live = estimate_option_iv(
            {"ticker": "AAA", "anchor_price": 110},
            [
                option_pair(110, strike, 0.60, live_years)
                for strike in (95, 97.5, 100, 102.5, 105, 107.5, 110, 112.5, 115)
            ],
            live_years,
            moneyness_targets=reference["iv_input_log_moneyness"],
        )

        self.assertEqual(reference["iv_input_strike_values"], [95, 97.5, 100, 102.5, 105])
        self.assertEqual(live["iv_input_strike_values"], [105, 107.5, 110, 112.5, 115])
        self.assertLess(live["iv_moneyness_match_max_error"], 0.005)
        self.assertAlmostEqual(reference["annualized_implied_vol_pct"], 50.0, places=6)
        self.assertAlmostEqual(live["annualized_implied_vol_pct"], 60.0, places=6)
        self.assertAlmostEqual(
            100
            * (
                live["annualized_implied_vol_pct"]
                / reference["annualized_implied_vol_pct"]
                - 1
            ),
            20.0,
            places=6,
        )

    def test_tracker_calculates_and_persists_last_reliable_close_value(self):
        reference_years = 7 / 365
        reference = estimate_option_iv(
            {"ticker": "AAA", "anchor_price": 100},
            [
                option_pair(100, strike, 0.50, reference_years)
                for strike in (95, 97.5, 100, 102.5, 105)
            ],
            reference_years,
        )
        row = reference_row(reference)
        event_time = datetime(2026, 8, 27, 15, 0, tzinfo=UTC)
        expiry_close = "2026-08-28T16:00:00-04:00"
        live_years = (
            datetime.fromisoformat(expiry_close).astimezone(UTC) - event_time
        ).total_seconds() / (365 * 24 * 60 * 60)

        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "iv.json"
            tracker = IvExpansionTracker(
                [row],
                expiry_close,
                "2026-08-27",
                cache,
            )
            tracker.update_spot("AAA", 110, event_time)
            changed = None
            for strike in (105, 107.5, 110, 112.5, 115):
                pair = option_pair(110, strike, 0.60, live_years)
                changed = tracker.update_option_quote(
                    pair["call"]["raw_symbol"],
                    pair["call"]["bid"],
                    pair["call"]["ask"],
                    event_time,
                )
                changed = tracker.update_option_quote(
                    pair["put"]["raw_symbol"],
                    pair["put"]["bid"],
                    pair["put"]["ask"],
                    event_time,
                ) or changed

            self.assertEqual(changed, "AAA")
            live_result = tracker.public_results(["AAA"], event_time)[0]
            self.assertAlmostEqual(live_result["iv_expansion_pct"], 20.0, places=5)
            self.assertEqual(live_result["iv_expansion_level"], 2)
            self.assertEqual(live_result["live_strikes"], [105, 107.5, 110, 112.5, 115])
            self.assertEqual(live_result["status"], "live")
            self.assertEqual(live_result["requested_session"], "2026-08-27")
            self.assertEqual(live_result["iv_reference_session"], "2026-08-26")
            self.assertEqual(live_result["iv_reference_status"], "yesterday")
            self.assertEqual(live_result["fallback_sessions"], 0)
            self.assertEqual(live_result["baseline_revision"], 2)

            after_close = datetime(2026, 8, 27, 21, 0, tzinfo=UTC)
            closed_result = tracker.public_results(["AAA"], after_close)[0]
            self.assertEqual(closed_result["status"], "close")
            self.assertFalse(live_result["iv_expansion_bucket_score"]["is_stale"])
            self.assertTrue(closed_result["iv_expansion_bucket_score"]["is_stale"])
            self.assertEqual(
                closed_result["iv_expansion_bucket_score"]["bucket"],
                live_result["iv_expansion_bucket_score"]["bucket"],
            )
            self.assertEqual(closed_result["iv_expansion_bucket_score"]["observation_date"], "2026-08-27")
            self.assertTrue(closed_result["is_persisted"])
            self.assertEqual(
                closed_result["iv_expansion_pct"],
                live_result["iv_expansion_pct"],
            )

            tracker.save_cache()
            restored = IvExpansionTracker(
                [row],
                expiry_close,
                "2026-08-27",
                cache,
            )
            restored.load_cache()
            self.assertEqual(len(restored.results), 1)

            next_session = IvExpansionTracker(
                [row],
                expiry_close,
                "2026-08-28",
                cache,
            )
            next_session.load_cache()
            self.assertEqual(next_session.results, {})

            refreshed_cycle = IvExpansionTracker(
                [row],
                expiry_close,
                "2026-08-27",
                cache,
                cycle_id="new-premarket-refresh",
            )
            refreshed_cycle.load_cache()
            self.assertEqual(refreshed_cycle.results, {})

    def test_latest_same_day_quotes_use_nearest_prior_spot_when_live_quotes_are_stale(self):
        reference_years = 7 / 365
        reference = estimate_option_iv(
            {"ticker": "AAA", "anchor_price": 100},
            [
                option_pair(100, strike, 0.50, reference_years)
                for strike in (95, 97.5, 100, 102.5, 105)
            ],
            reference_years,
        )
        spot_time = datetime(2026, 8, 27, 15, 0, tzinfo=UTC)
        quote_time = datetime(2026, 8, 27, 15, 10, tzinfo=UTC)
        expiry_close = "2026-08-28T16:00:00-04:00"
        live_years = (
            datetime.fromisoformat(expiry_close).astimezone(UTC) - quote_time
        ).total_seconds() / (365 * 24 * 60 * 60)

        with tempfile.TemporaryDirectory() as directory:
            tracker = IvExpansionTracker(
                [reference_row(reference)],
                expiry_close,
                "2026-08-27",
                Path(directory) / "iv.json",
            )
            tracker.update_spot("AAA", 110, spot_time)
            for strike in (105, 107.5, 110, 112.5, 115):
                pair = option_pair(110, strike, 0.60, live_years)
                for side in ("call", "put"):
                    tracker.update_option_quote(
                        pair[side]["raw_symbol"],
                        pair[side]["bid"],
                        pair[side]["ask"],
                        quote_time,
                    )

            result = tracker.public_results(["AAA"], quote_time)[0]
            self.assertAlmostEqual(result["iv_expansion_pct"], 20.0, places=5)
            self.assertEqual(result["status"], "live")
            self.assertEqual(result["calculation_source"], "latest_same_day_quotes")
            self.assertEqual(result["last_valid_at"], quote_time.isoformat())
            self.assertEqual(result["spot_timestamp"], spot_time.isoformat())
            self.assertEqual(result["spot_quote_skew_seconds"], 600)
            self.assertEqual(result["spot_age_seconds"], 600)

            next_day_open = datetime(2026, 8, 28, 15, 0, tzinfo=UTC)
            self.assertEqual(
                tracker.public_results(["AAA"], next_day_open)[0]["status"],
                "close",
            )

    def test_latest_quote_fallback_rejects_a_later_equity_price(self):
        reference_years = 7 / 365
        reference = estimate_option_iv(
            {"ticker": "AAA", "anchor_price": 100},
            [
                option_pair(100, strike, 0.50, reference_years)
                for strike in (95, 97.5, 100, 102.5, 105)
            ],
            reference_years,
        )
        quote_time = datetime(2026, 8, 27, 15, 0, tzinfo=UTC)
        spot_time = datetime(2026, 8, 27, 15, 10, tzinfo=UTC)
        expiry_close = "2026-08-28T16:00:00-04:00"
        live_years = (
            datetime.fromisoformat(expiry_close).astimezone(UTC) - quote_time
        ).total_seconds() / (365 * 24 * 60 * 60)

        with tempfile.TemporaryDirectory() as directory:
            tracker = IvExpansionTracker(
                [reference_row(reference)],
                expiry_close,
                "2026-08-27",
                Path(directory) / "iv.json",
            )
            pair = option_pair(110, 110, 0.60, live_years)
            for side in ("call", "put"):
                tracker.update_option_quote(
                    pair[side]["raw_symbol"],
                    pair[side]["bid"],
                    pair[side]["ask"],
                    quote_time,
                )

            self.assertFalse(tracker.update_spot("AAA", 110, spot_time))
            self.assertEqual(tracker.results, {})

    def test_small_future_spot_skew_is_allowed_for_feed_ordering(self):
        reference_years = 7 / 365
        reference = estimate_option_iv(
            {"ticker": "AAA", "anchor_price": 100},
            [option_pair(100, 100, 0.50, reference_years)],
            reference_years,
        )
        quote_time = datetime(2026, 8, 27, 15, 0, tzinfo=UTC)
        spot_time = datetime(2026, 8, 27, 15, 0, 3, tzinfo=UTC)
        expiry_close = "2026-08-28T16:00:00-04:00"
        live_years = (
            datetime.fromisoformat(expiry_close).astimezone(UTC) - quote_time
        ).total_seconds() / (365 * 24 * 60 * 60)

        with tempfile.TemporaryDirectory() as directory:
            tracker = IvExpansionTracker(
                [reference_row(reference)],
                expiry_close,
                "2026-08-27",
                Path(directory) / "iv.json",
            )
            pair = option_pair(110, 110, 0.60, live_years)
            for side in ("call", "put"):
                tracker.update_option_quote(
                    pair[side]["raw_symbol"],
                    pair[side]["bid"],
                    pair[side]["ask"],
                    quote_time,
                )

            self.assertTrue(tracker.update_spot("AAA", 110, spot_time))
            result = tracker.results["AAA"]
            self.assertEqual(result["spot_timestamp"], spot_time.isoformat())
            self.assertEqual(result["spot_age_seconds"], -3)

    def test_latest_quote_fallback_does_not_mix_trading_days(self):
        reference_years = 7 / 365
        reference = estimate_option_iv(
            {"ticker": "AAA", "anchor_price": 100},
            [
                option_pair(100, strike, 0.50, reference_years)
                for strike in (95, 97.5, 100, 102.5, 105)
            ],
            reference_years,
        )
        quote_time = datetime(2026, 8, 26, 15, 0, tzinfo=UTC)
        spot_time = datetime(2026, 8, 27, 15, 10, tzinfo=UTC)
        expiry_close = "2026-08-28T16:00:00-04:00"
        live_years = (
            datetime.fromisoformat(expiry_close).astimezone(UTC) - quote_time
        ).total_seconds() / (365 * 24 * 60 * 60)

        with tempfile.TemporaryDirectory() as directory:
            tracker = IvExpansionTracker(
                [reference_row(reference)],
                expiry_close,
                "2026-08-27",
                Path(directory) / "iv.json",
            )
            pair = option_pair(110, 110, 0.60, live_years)
            for side in ("call", "put"):
                tracker.update_option_quote(
                    pair[side]["raw_symbol"],
                    pair[side]["bid"],
                    pair[side]["ask"],
                    quote_time,
                )

            self.assertFalse(tracker.update_spot("AAA", 110, spot_time))
            self.assertEqual(tracker.results, {})

    def test_three_star_thresholds_and_contract_generation(self):
        self.assertEqual(
            [iv_expansion_level(value) for value in (6, 10, 19.9, 20, 29.9, 30)],
            [0, 1, 1, 2, 2, 3],
        )
        symbols = option_contract_symbols([{
            "ticker": "AAA",
            "option_expiry": "2026-08-28",
            "iv_expansion_reference": {"option_expiry": "2026-08-28"},
            "iv_chain": {"occ_root": "AAA", "strikes": [105, 107.5]},
        }])
        self.assertEqual(symbols, [
            "AAA   260828C00105000",
            "AAA   260828C00107500",
            "AAA   260828P00105000",
            "AAA   260828P00107500",
        ])


if __name__ == "__main__":
    unittest.main()

class MissingIvCacheTests(unittest.TestCase):
    def test_missing_iv_file_reconstructs_from_matching_observation(self):
        from scripts.iv_expansion_history import initialize_database, upsert_observation
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            connection=initialize_database(root/'iv-expansion-history-session-batch.sqlite3')
            upsert_observation(connection, {
                'ticker':'AAA','session_date':'2026-08-27','bucket':'15:30',
                'observation_ts':'2026-08-27T15:59:00-04:00','requested_reference_session':'2026-08-26',
                'reference_session':'2026-08-26','reference_status':'yesterday','fallback_sessions':0,
                'expiry':'2026-08-28','expiry_status':'target_weekly','observation_dte_days':1,
                'observation_tenor_class':'front_weekly','reference_iv_pct':50,'observed_iv_pct':55,
                'iv_expansion_pct':10,'iv_log_change_pct':100*math.log(1.1),
                'reference_reliability':'high','observed_reliability':'high','moneyness_match_max_error':0.001})
            connection.commit();connection.close()
            row=reference_row({'annualized_implied_vol_pct':50,'forward_price':100,
                               'iv_input_strike_values':[100],'iv_input_log_moneyness':[0]})
            tracker=IvExpansionTracker(rows=[row],option_expiry_close='2026-08-28T16:00:00-04:00',
                                       anchor_date='2026-08-27',cache_path=root/'live-iv-expansion.json')
            tracker.load_cache()
            self.assertEqual(tracker.results['AAA']['iv_expansion_pct'],10)
            self.assertTrue(tracker.cache_path.exists())
