import asyncio
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from scripts.databento_config import (
    resolve_historical_api_keys,
    resolve_live_api_key,
)
from scripts.live_price_adapter import DatabentoFeed, FeedConfig, PriceStore
from scripts.nasdaq_universe_movers import create_historical_clients


class DatabentoKeyRoutingIntegrationTests(unittest.TestCase):
    def test_role_specific_keys_override_legacy_key(self):
        environ = {
            "DATABENTO_LIVE_API_KEY": "live-test-key",
            "DATABENTO_OPTIONS_API_KEY": "options-test-key",
            "DATABENTO_API_KEY": "legacy-test-key",
        }

        self.assertEqual(resolve_live_api_key(environ), "live-test-key")
        self.assertEqual(
            resolve_historical_api_keys(environ),
            ("live-test-key", "options-test-key"),
        )

    def test_sdk_clients_receive_their_configured_role_keys(self):
        environ = {
            "DATABENTO_LIVE_API_KEY": "live-test-key",
            "DATABENTO_OPTIONS_API_KEY": "options-test-key",
        }
        live_key = resolve_live_api_key(environ)
        self.assertIsNotNone(live_key)

        with tempfile.TemporaryDirectory() as tempdir:
            feed = DatabentoFeed(
                api_key=live_key,
                config=FeedConfig("EQUS.MINI", frozenset({"regular"})),
                symbols=["AAPL"],
                store=PriceStore(["AAPL"], Path(tempdir) / "prices.json"),
                replay_minutes=1,
                stop=asyncio.Event(),
            )
            with patch("scripts.live_price_adapter.db.Live") as live_client:
                feed.connect()
                self.assertEqual(live_client.call_args.kwargs["key"], "live-test-key")

        with patch("scripts.nasdaq_universe_movers.db.Historical") as historical_client:
            create_historical_clients(environ)
            self.assertEqual(
                [call.args[0] for call in historical_client.call_args_list],
                ["live-test-key", "options-test-key"],
            )

    def test_afterhours_connection_recovers_missing_regular_closes_with_minute_bars(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = PriceStore(["AAPL"], Path(tempdir) / "prices.json")
            feed = DatabentoFeed(
                api_key="live-test-key",
                config=FeedConfig(
                    "EQUS.MINI",
                    frozenset({"premarket", "regular", "afterhours"}),
                ),
                symbols=["AAPL"],
                store=store,
                replay_minutes=60,
                stop=asyncio.Event(),
            )
            afterhours = datetime(2026, 8, 28, 22, 0, tzinfo=UTC)
            with (
                patch("scripts.live_price_adapter.utc_now", return_value=afterhours),
                patch("scripts.live_price_adapter.db.Live") as live_client,
            ):
                feed.connect()

            subscriptions = live_client.return_value.subscribe.call_args_list
            self.assertEqual(len(subscriptions), 2)
            self.assertEqual(subscriptions[0].kwargs["schema"], "ohlcv-1s")
            self.assertEqual(subscriptions[1].kwargs["schema"], "ohlcv-1m")
            self.assertEqual(subscriptions[1].kwargs["symbols"], ["AAPL"])
            self.assertEqual(
                subscriptions[1].kwargs["start"],
                datetime(2026, 8, 28, 19, 30, tzinfo=UTC),
            )


if __name__ == "__main__":
    unittest.main()
