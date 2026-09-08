import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from scripts.historical_iv_backfill_probe import (
    choose_target_expiry,
    load_tickers_from_source,
    parse_tickers,
    pick_evenly,
    quotes_for_ticker,
    snapshot_bounds,
    trading_sessions,
)


class HistoricalIvBackfillProbeTests(unittest.TestCase):
    def test_parse_tickers_dedupes_and_normalizes(self):
        self.assertEqual(
            parse_tickers([" aapl, msft ", "AAPL", "BRK.B"]),
            ["AAPL", "MSFT", "BRKB"],
        )

    def test_loads_tickers_from_iv_universe_shape(self):
        payload = {
            "today": {
                "rows": [
                    {"ticker": "AAPL"},
                    {"ticker": "MSFT"},
                    {"ticker": "AAPL"},
                ]
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coverage.json"
            path.write_text(json.dumps(payload))
            self.assertEqual(load_tickers_from_source(path), ["AAPL", "MSFT"])
            self.assertEqual(load_tickers_from_source(path, limit=1), ["AAPL"])

    def test_trading_sessions_are_xnys_sessions(self):
        sessions = trading_sessions(
            pd.Timestamp("2026-08-24"),
            pd.Timestamp("2026-08-28"),
            years=2,
        )
        self.assertEqual(sessions, [
            date(2026, 8, 24),
            date(2026, 8, 25),
            date(2026, 8, 26),
            date(2026, 8, 27),
            date(2026, 8, 28),
        ])

    def test_pick_evenly_handles_one_and_many_samples(self):
        sessions = [date(2026, 8, day) for day in range(24, 29)]
        self.assertEqual(pick_evenly(sessions, 0), [])
        self.assertEqual(pick_evenly(sessions, 1), [date(2026, 8, 26)])
        self.assertEqual(
            pick_evenly(sessions, 3),
            [date(2026, 8, 24), date(2026, 8, 26), date(2026, 8, 28)],
        )

    def test_snapshot_bounds_are_eastern_time(self):
        start, end = snapshot_bounds(date(2026, 8, 28), "15:59:00", 61)
        self.assertEqual(start, "2026-08-28T15:59:00-04:00")
        self.assertEqual(end, "2026-08-28T16:00:01-04:00")

    def test_choose_target_expiry_requires_paired_sides_near_target_dte(self):
        quotes = pd.DataFrame({
            "symbol": [
                "AAPL  260904C00200000",
                "AAPL  260904P00200000",
                "AAPL  260925C00200000",
                "AAPL  260925P00200000",
                "AAPL  261016C00200000",
                "MSFT  260925P00400000",
            ]
        })

        self.assertEqual(
            choose_target_expiry(
                quotes,
                "AAPL",
                date(2026, 8, 28),
                target_dte=30,
                min_dte=7,
                max_dte=60,
            ),
            date(2026, 9, 25),
        )

    def test_quotes_for_ticker_filters_other_option_roots(self):
        quotes = pd.DataFrame({
            "symbol": [
                "AAPL  260925C00200000",
                "MSFT  260925P00400000",
                "not-an-option",
            ],
            "bid_px_00": [1.0, 2.0, 3.0],
        })

        filtered = quotes_for_ticker(quotes, "AAPL")

        self.assertEqual(filtered["symbol"].tolist(), ["AAPL  260925C00200000"])


if __name__ == "__main__":
    unittest.main()
