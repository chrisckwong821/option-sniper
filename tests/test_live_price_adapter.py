import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.live_price_adapter import (
    PriceStore,
    market_session_at,
    tracked_symbols,
    trading_date_at,
)


class MarketSessionTests(unittest.TestCase):
    def test_us_equity_sessions(self):
        self.assertEqual(market_session_at(datetime(2026, 8, 28, 12, 0, tzinfo=UTC)), "premarket")
        self.assertEqual(market_session_at(datetime(2026, 8, 28, 13, 30, tzinfo=UTC)), "regular")
        self.assertEqual(market_session_at(datetime(2026, 8, 28, 21, 0, tzinfo=UTC)), "afterhours")
        self.assertEqual(market_session_at(datetime(2026, 8, 28, 2, 0, tzinfo=UTC)), "overnight")

    def test_friday_night_is_closed(self):
        friday_8pm_et = datetime(2026, 8, 29, 0, 0, tzinfo=UTC)
        self.assertEqual(market_session_at(friday_8pm_et), "closed")

    def test_overnight_print_uses_next_trading_date(self):
        thursday_10pm_et = datetime(2026, 8, 28, 2, 0, tzinfo=UTC)
        self.assertEqual(trading_date_at(thursday_10pm_et), "2026-08-28")


class PriceStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = PriceStore(
            symbols=["PANW", "COIN"],
            cache_path=Path(self.tempdir.name) / "prices.json",
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_rejects_unknown_closed_and_out_of_order_prices(self):
        first = datetime(2026, 8, 28, 13, 30, tzinfo=UTC)
        self.assertFalse(self.store.update_price("OTHER", 10, first, "EQUS.MINI"))
        self.assertTrue(self.store.update_price("PANW", 390, first, "EQUS.MINI"))
        self.assertFalse(
            self.store.update_price("PANW", 389, first - timedelta(seconds=1), "EQUS.MINI")
        )
        friday_night = datetime(2026, 8, 29, 0, 0, tzinfo=UTC)
        self.assertFalse(self.store.update_price("COIN", 200, friday_night, "OCEA.MEMOIR"))
        self.assertEqual(self.store.prices["PANW"]["price"], 390)

    def test_marks_old_extended_hours_print_stale(self):
        event_time = datetime(2026, 8, 28, 2, 0, tzinfo=UTC)
        self.assertTrue(self.store.update_price("COIN", 200, event_time, "OCEA.MEMOIR"))
        self.assertEqual(self.store.prices["COIN"]["trading_date"], "2026-08-28")
        self.assertEqual(self.store.prices["COIN"]["session"], "overnight")
        public = self.store.public_price(
            self.store.prices["COIN"],
            event_time + timedelta(minutes=16),
        )
        self.assertEqual(public["session"], "overnight")
        self.assertTrue(public["is_stale"])

    def test_premarket_price_is_tagged_for_friday_ranking(self):
        friday_8am_et = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
        self.assertTrue(
            self.store.update_price("PANW", 390, friday_8am_et, "EQUS.MINI")
        )
        point = self.store.prices["PANW"]
        self.assertEqual(point["session"], "premarket")
        self.assertEqual(point["trading_date"], "2026-08-28")
        self.assertTrue(point["is_extended_hours"])

    def test_cache_round_trip(self):
        event_time = datetime(2026, 8, 28, 13, 30, tzinfo=UTC)
        self.store.update_price("PANW", 390, event_time, "EQUS.MINI", 100)
        self.store.save_cache()
        restored = PriceStore(["PANW"], self.store.cache_path)
        restored.load_cache()
        self.assertEqual(restored.prices["PANW"]["price"], 390)


class SymbolUniverseTests(unittest.TestCase):
    def test_tracks_union_of_fixed_and_iv_universe(self):
        fixed = {"rows": [{"ticker": "PANW"}, {"ticker": "NVDA"}]}
        universe = {"rows": [{"ticker": "AAPL"}, {"ticker": "NVDA"}]}
        self.assertEqual(tracked_symbols(fixed, universe), ["AAPL", "NVDA", "PANW"])


if __name__ == "__main__":
    unittest.main()
