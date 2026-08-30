import argparse
import contextlib
import io
import math
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
from databento.common.error import BentoServerError

from scripts.nasdaq_universe_movers import (
    RISK_FREE_RATE,
    black76_straddle,
    build_spot_candidates,
    estimate_option_iv,
    fetch_option_pairs,
    fetch_spot,
    implied_total_vol,
    is_comparable_stock,
    load_universe,
    parse_occ_symbol,
    prepare_option_pairs,
    rank_by_implied_move,
    resolve_run_config,
)


def option_pair(
    strike,
    total_vol=0.0282842712474619,
    spread=0.04,
    year_fraction=2 / 365,
):
    forward = 100.0
    discount_factor = math.exp(-RISK_FREE_RATE * year_fraction)
    straddle = black76_straddle(forward, strike, total_vol, discount_factor)
    parity = discount_factor * (forward - strike)
    call_mid = 0.5 * (straddle + parity)
    put_mid = 0.5 * (straddle - parity)

    def quote(mid):
        return {
            "bid": max(0.0, mid - spread / 2),
            "ask": mid + spread / 2,
            "mid": mid,
            "spread": spread,
            "raw_symbol": "synthetic",
        }

    return {"strike": float(strike), "call": quote(call_mid), "put": quote(put_mid)}


def candidate(ticker, end_close):
    log_return_pct = 100 * math.log(end_close / 100)
    return {
        "ticker": ticker,
        "company_name": f"{ticker} Corp.",
        "sector": "Technology",
        "industry": "Software",
        "market_cap_usd": 20_000_000_000,
        "return_anchor": "2026-08-26 close",
        "anchor_price": 100.0,
        "end_close": end_close,
        "weekly_return_pct": end_close - 100,
        "weekly_log_return_pct": log_return_pct,
        "minimum_daily_notional_usd": 50_000_000,
        "average_daily_notional_usd": 55_000_000,
        "path": [
            {
                "date": "2026-08-26",
                "close": 100.0,
                "cumulative_log_return_pct": 0.0,
                "daily_notional_usd": 50_000_000,
            },
            {
                "date": "2026-08-27",
                "close": end_close,
                "cumulative_log_return_pct": log_return_pct,
                "daily_notional_usd": 60_000_000,
            },
        ],
    }


class RunConfigTests(unittest.TestCase):
    def test_liquidity_sessions_exclude_exchange_holidays(self):
        args = argparse.Namespace(
            window_start=pd.Timestamp("2026-09-04"),
            window_end=pd.Timestamp("2026-09-08"),
            option_expiry=pd.Timestamp("2026-09-11"),
            option_expiry_close=None,
            spot_dataset="EQUS.MINI",
            return_anchor_field="close",
            option_snapshot_start=None,
            option_snapshot_end=None,
            option_valuation_time=None,
            endpoint_time=None,
            option_cache_dir=None,
        )

        config = resolve_run_config(args)

        self.assertEqual(
            [str(value) for value in config["week_dates"]],
            ["2026-09-04", "2026-09-08"],
        )


class UniverseFilterTests(unittest.TestCase):
    def test_excludes_non_comparable_instruments_and_small_caps(self):
        self.assertTrue(is_comparable_stock("Acme Common Stock"))
        self.assertTrue(is_comparable_stock("Acme American Depositary Shares"))
        self.assertFalse(is_comparable_stock("Acme Warrants"))
        self.assertFalse(is_comparable_stock("Acme Units"))

        payload = {"data": {"rows": [
            {
                "symbol": "AAA",
                "name": "Alpha Common Stock",
                "marketCap": "12000000000",
                "sector": "Technology",
                "industry": "Software",
                "country": "United States",
            },
            {"symbol": "SMOL", "name": "Small Common Stock", "marketCap": "100"},
            {"symbol": "WRT", "name": "Warrant Co Warrants", "marketCap": "15000000000"},
            {"symbol": "BAD", "name": "Bad Cap", "marketCap": "not-a-number"},
        ]}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "universe.json"
            path.write_text(__import__("json").dumps(payload))
            eligible, exclusions, raw_count = load_universe(path, 10_000_000_000)

        self.assertEqual([row["symbol"] for row in eligible], ["AAA"])
        self.assertEqual(raw_count, 4)
        self.assertEqual(exclusions, {
            "market_cap": 1,
            "instrument_type": 1,
            "invalid_market_cap": 1,
        })

    def test_spot_candidates_enforce_sessions_liquidity_and_target_cap(self):
        sessions = [date(2026, 8, 26), date(2026, 8, 27)]
        history = pd.DataFrame([
            {"symbol": "AAA", "date": sessions[0], "open": 99, "close": 100, "volume": 600_000},
            {"symbol": "AAA", "date": sessions[1], "open": 100, "close": 105, "volume": 700_000},
            {"symbol": "BBB", "date": sessions[0], "open": 50, "close": 50, "volume": 1_000},
            {"symbol": "BBB", "date": sessions[1], "open": 50, "close": 51, "volume": 1_000},
        ])
        universe = [
            {"symbol": "AAA", "name": "Alpha", "sector": "Tech", "industry": "Software", "market_cap": 20e9},
            {"symbol": "BBB", "name": "Beta", "sector": "Tech", "industry": "Software", "market_cap": 15e9},
        ]

        qualified, selected, effective, capped, exclusions = build_spot_candidates(
            history,
            universe,
            daily_notional_min=25_000_000,
            candidate_target=1,
            window_start=sessions[0],
            week_dates=sessions,
            anchor_label="last trade",
            anchor_field="close",
        )

        self.assertEqual([row["ticker"] for row in qualified], ["AAA"])
        self.assertEqual([row["ticker"] for row in selected], ["AAA"])
        self.assertAlmostEqual(selected[0]["weekly_return_pct"], 5.0)
        self.assertEqual(effective, 25_000_000)
        self.assertFalse(capped)
        self.assertEqual(exclusions["liquidity"], 1)

    def test_spot_candidates_can_disable_liquidity_gate_and_target_cap(self):
        sessions = [date(2026, 8, 26), date(2026, 8, 27)]
        history = pd.DataFrame([
            {"symbol": symbol, "date": session, "open": 100, "close": 100, "volume": volume}
            for symbol, volume in (("ZZZ", 1), ("AAA", 0))
            for session in sessions
        ])
        universe = [
            {"symbol": symbol, "name": symbol, "sector": "Tech", "industry": "Chips", "market_cap": 6e9}
            for symbol in ("ZZZ", "AAA")
        ]

        qualified, selected, effective, capped, exclusions = build_spot_candidates(
            history,
            universe,
            daily_notional_min=None,
            candidate_target=None,
            window_start=sessions[0],
            week_dates=sessions,
            anchor_label="official close",
            anchor_field="close",
        )

        self.assertEqual([row["ticker"] for row in qualified], ["AAA", "ZZZ"])
        self.assertEqual(selected, qualified)
        self.assertIsNone(effective)
        self.assertFalse(capped)
        self.assertEqual(exclusions["liquidity"], 0)

    def test_spot_cost_guard_stops_before_download(self):
        client = MagicMock()
        client.metadata.get_cost.return_value = 1.01
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "exceeds --max-spot-cost"):
                fetch_spot(
                    client,
                    ["AAA"],
                    "2026-08-26",
                    "2026-08-27",
                    "EQUS.MINI",
                    1.0,
                )
        client.timeseries.get_range.assert_not_called()

    def test_summary_spot_uses_consolidated_daily_bars(self):
        frame = pd.DataFrame([{
            "ts_event": pd.Timestamp("2026-08-28", tz="UTC"),
            "symbol": "TSEM",
            "open": 219.36,
            "high": 220.5695,
            "low": 206.9099,
            "close": 208.01,
            "volume": 1_250_752,
        }]).set_index("ts_event")
        client = MagicMock()
        client.metadata.get_cost.return_value = 0
        client.timeseries.get_range.return_value.to_df.return_value = frame

        with contextlib.redirect_stdout(io.StringIO()):
            history, cost, schema = fetch_spot(
                client,
                ["TSEM"],
                "2026-08-28",
                "2026-08-29",
                "EQUS.SUMMARY",
                1.0,
            )

        self.assertEqual(schema, "ohlcv-1d")
        self.assertEqual(cost, 0)
        self.assertEqual(history.loc[0, "volume"], 1_250_752)
        self.assertEqual(str(history.loc[0, "date"]), "2026-08-28")
        request = client.timeseries.get_range.call_args.kwargs
        self.assertEqual(request["dataset"], "EQUS.SUMMARY")
        self.assertEqual(request["schema"], "ohlcv-1d")

    def test_option_cost_preflight_splits_gateway_timeouts(self):
        client = MagicMock()
        client.metadata.get_cost.side_effect = [
            BentoServerError(504),
            0.6,
            0.7,
        ]
        candidates = [{"ticker": ticker} for ticker in ("AAA", "BBB", "CCC", "DDD")]

        with tempfile.TemporaryDirectory() as directory:
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "exceeds --max-option-cost"):
                    fetch_option_pairs(
                        client,
                        candidates,
                        "2026-08-27T15:59:00-04:00",
                        "2026-08-27T16:00:01-04:00",
                        date(2026, 9, 4),
                        max_cost=1.0,
                        known_cost=None,
                        chunk_size=4,
                        cache_dir=Path(directory),
                    )

        self.assertEqual(client.metadata.get_cost.call_count, 3)
        client.timeseries.get_range.assert_not_called()


class OptionMathTests(unittest.TestCase):
    def test_occ_parser_and_invalid_symbols(self):
        parsed = parse_occ_symbol("AAPL  260828C00200000")
        self.assertEqual(parsed["root"], "AAPL")
        self.assertEqual(parsed["expiry"], date(2026, 8, 28))
        self.assertEqual(parsed["side"], "C")
        self.assertEqual(parsed["strike"], 200.0)
        self.assertIsNone(parse_occ_symbol("not-an-option"))
        self.assertIsNone(parse_occ_symbol("AAPL  991399C00200000"))

    def test_black76_inversion_round_trip(self):
        expected_total_vol = 0.18
        discount_factor = math.exp(-RISK_FREE_RATE * 14 / 365)
        straddle = black76_straddle(102.0, 100.0, expected_total_vol, discount_factor)
        actual = implied_total_vol(102.0, 100.0, straddle, discount_factor)
        self.assertAlmostEqual(actual, expected_total_vol, places=10)
        self.assertIsNone(implied_total_vol(100, 100, 0, discount_factor))

    def test_amzn_snapshot_uses_four_percent_discount_rate(self):
        def pair(strike, call_mid, put_mid, call_spread, put_spread):
            def quote(mid, spread):
                return {
                    "bid": mid - spread / 2,
                    "ask": mid + spread / 2,
                    "mid": mid,
                    "spread": spread,
                    "raw_symbol": "AMZN synthetic regression quote",
                }

            return {
                "strike": strike,
                "call": quote(call_mid, call_spread),
                "put": quote(put_mid, put_spread),
            }

        pairs = [
            pair(255.0, 4.975, 3.45, 0.25, 0.20),
            pair(257.5, 3.65, 4.70, 0.20, 0.20),
            pair(252.5, 6.50, 2.48, 0.30, 0.16),
            pair(260.0, 2.675, 6.175, 0.13, 0.25),
            pair(250.0, 8.325, 1.725, 0.45, 0.13),
            pair(262.5, 1.90, 7.80, 0.08, 0.50),
            pair(247.5, 10.275, 1.17, 0.55, 0.10),
            pair(265.0, 1.325, 9.80, 0.05, 0.40),
            pair(245.0, 12.40, 0.80, 0.60, 0.06),
        ]
        amzn = candidate("AMZN", 266.43)
        amzn["anchor_price"] = 256.26

        estimate = estimate_option_iv(amzn, pairs, 8 / 365)

        self.assertAlmostEqual(estimate["risk_free_rate_pct"], 4.0)
        self.assertAlmostEqual(estimate["forward_price"], 256.523525929, places=8)
        self.assertAlmostEqual(estimate["annualized_implied_vol_pct"], 27.550526627, places=8)
        self.assertEqual(estimate["iv_reliability"], "high")

    def test_quote_pairing_uses_latest_quote_and_reports_rejections(self):
        quotes = pd.DataFrame([
            {"ts": "2026-08-26T19:59:00Z", "symbol": "AAA   260828C00100000", "bid_px_00": 1.0, "ask_px_00": 2.0},
            {"ts": "2026-08-26T20:00:00Z", "symbol": "AAA   260828C00100000", "bid_px_00": 1.2, "ask_px_00": 1.4},
            {"ts": "2026-08-26T20:00:00Z", "symbol": "AAA   260828P00100000", "bid_px_00": 1.1, "ask_px_00": 1.3},
            {"ts": "2026-08-26T20:00:00Z", "symbol": "AAA   260904C00100000", "bid_px_00": 1.0, "ask_px_00": 1.2},
            {"ts": "2026-08-26T20:00:00Z", "symbol": "broken", "bid_px_00": 1.0, "ask_px_00": 1.2},
            {"ts": "2026-08-26T20:00:00Z", "symbol": "AAA   260828C00105000", "bid_px_00": 2.0, "ask_px_00": 1.0},
        ])
        pairs, diagnostics = prepare_option_pairs(
            quotes,
            [{"ticker": "AAA"}],
            date(2026, 8, 28),
        )

        self.assertEqual(len(pairs["AAA"]), 1)
        self.assertAlmostEqual(pairs["AAA"][0]["call"]["mid"], 1.3)
        self.assertEqual(diagnostics, {
            "unparsed_records": 1,
            "wrong_expiry_records": 1,
            "invalid_quote_records": 1,
        })

    def test_iv_reliability_tracks_strike_count_and_spread(self):
        base = candidate("AAA", 105)
        year_fraction = 2 / 365

        high = estimate_option_iv(base, [option_pair(98), option_pair(100), option_pair(102)], year_fraction)
        medium = estimate_option_iv(base, [option_pair(99, spread=0.5), option_pair(101, spread=0.5)], year_fraction)
        low = estimate_option_iv(base, [option_pair(100)], year_fraction)

        self.assertEqual(high["iv_reliability"], "high")
        self.assertEqual(high["iv_input_strikes"], 3)
        self.assertAlmostEqual(high["annualized_implied_vol_pct"], 38.2099, places=3)
        self.assertEqual(medium["iv_reliability"], "medium")
        self.assertEqual(low["iv_reliability"], "low")

    def test_realized_horizon_ranking_uses_option_implied_sigma(self):
        pairs = [option_pair(98), option_pair(100), option_pair(102)]
        ranked, top, manifest, option_years, realized_years = rank_by_implied_move(
            [candidate("AAA", 105), candidate("BBB", 102)],
            {"AAA": pairs, "BBB": pairs},
            option_expiry=date(2026, 8, 28),
            option_expiry_close="2026-08-28T16:00:00-04:00",
            valuation_time="2026-08-26T16:00:00-04:00",
            endpoint_time="2026-08-27T16:00:00-04:00",
            limit=20,
            ranking_horizon="realized_window",
            direction="gainers",
        )

        self.assertEqual([row["ticker"] for row in top], ["AAA", "BBB"])
        self.assertEqual(top[0]["rank"], 1)
        self.assertAlmostEqual(top[0]["realized_horizon_implied_sigma_pct"], 2.0, places=6)
        self.assertAlmostEqual(top[0]["realized_horizon_implied_z"], math.log(1.05) / 0.02, places=6)
        self.assertTrue(top[0]["outside_one_implied_sigma"])
        self.assertEqual(len(ranked), 2)
        self.assertEqual(len(manifest), 2)
        self.assertAlmostEqual(option_years, 2 / 365)
        self.assertAlmostEqual(realized_years, 1 / 365)


if __name__ == "__main__":
    unittest.main()
