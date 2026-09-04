import math
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from scripts.discord_zscore_alert import (
    AlertEngine,
    AlertStore,
    AlertUniverse,
    DEFAULT_DISCORD_CHANNEL_ID,
    DefinitiveSendError,
    alert_cycle_date_at,
    build_universe,
    calculate_z_score,
    websocket_url,
)


def price_for_z(z_score, anchor=100.0, sigma_pct=2.0):
    return anchor * math.exp(z_score * sigma_pct / 100)


def price_point(ticker, z_score, timestamp, session="premarket", stale=False):
    return {
        "ticker": ticker,
        "price": price_for_z(z_score),
        "ts_event": timestamp,
        "session": session,
        "is_stale": stale,
    }


class AlertCycleTests(unittest.TestCase):
    def test_cycle_starts_at_four_am_eastern(self):
        self.assertEqual(
            alert_cycle_date_at(datetime(2026, 8, 29, 7, 59, tzinfo=UTC)),
            "2026-08-28",
        )
        self.assertEqual(
            alert_cycle_date_at(datetime(2026, 8, 29, 8, 0, tzinfo=UTC)),
            "2026-08-29",
        )

    def test_z_score_matches_dashboard_formula(self):
        self.assertAlmostEqual(calculate_z_score(price_for_z(1.25), 100, 2), 1.25)
        self.assertIsNone(calculate_z_score(100, 100, 0))

    def test_urls_are_derived_from_backend(self):
        self.assertEqual(
            websocket_url("https://tape.example.com/"),
            "wss://tape.example.com/ws/prices",
        )

    def test_repository_has_the_requested_default_channel(self):
        self.assertEqual(DEFAULT_DISCORD_CHANNEL_ID, "1544929277009465374")

    def test_universe_uses_next_market_session(self):
        universe = build_universe({
            "refresh_plan": {"next_session": "2026-08-28"},
            "today": {"rows": [{"ticker": "AAA"}]},
        })
        self.assertEqual(universe.target_session, "2026-08-28")
        self.assertEqual(list(universe.rows), ["AAA"])


class AlertEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "alerts.sqlite3"
        self.store = AlertStore(self.database)
        self.sent = []
        self.next_message_id = 100

        async def send(observation, threshold):
            self.sent.append((observation, threshold))
            self.next_message_id += 1
            return str(self.next_message_id)

        self.send = send
        self.engine = AlertEngine(self.store, self.send)
        self.universe = AlertUniverse(
            target_session="2026-08-28",
            rows={
                ticker: {
                    "ticker": ticker,
                    "company_name": f"{ticker} Inc.",
                    "live_anchor_price": 100,
                    "sigma_1d_pct": 2,
                    "iv_reliability": "high",
                }
                for ticker in ("AAA", "BBB")
            },
        )

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    async def process(self, *points):
        return await self.engine.process_message(
            {"type": "delta", "prices": list(points)},
            self.universe,
        )

    async def test_fresh_snapshot_alerts_every_ticker_already_above_threshold(self):
        sent = await self.engine.process_message(
            {
                "type": "snapshot",
                "prices": [
                    price_point("AAA", 1.01, "2026-08-28T12:00:00+00:00"),
                    price_point("BBB", 1.50, "2026-08-28T12:00:01+00:00"),
                ],
            },
            self.universe,
        )

        self.assertEqual(sent, 2)
        self.assertEqual([item[0].ticker for item in self.sent], ["AAA", "BBB"])
        self.assertEqual(self.store.count(), 2)
        self.assertEqual(self.store.summary("2026-08-28"), {
            "count": 2,
            "statuses": {"sent": 2},
            "sent_tickers": ["AAA", "BBB"],
        })

    async def test_crossing_recrossing_and_replayed_prices_alert_only_once(self):
        self.assertEqual(
            await self.process(price_point("AAA", 0.99, "2026-08-28T12:00:00+00:00")),
            0,
        )
        self.assertEqual(
            await self.process(price_point("AAA", 1.00, "2026-08-28T12:00:01+00:00")),
            1,
        )
        await self.process(price_point("AAA", 1.20, "2026-08-28T12:00:02+00:00"))
        await self.process(price_point("AAA", 0.80, "2026-08-28T12:00:03+00:00"))
        self.assertEqual(
            await self.process(price_point("AAA", 1.30, "2026-08-28T12:00:04+00:00")),
            0,
        )
        self.assertEqual(len(self.sent), 1)

    async def test_all_four_feed_sessions_belong_to_the_same_cycle(self):
        cases = [
            ("premarket", "2026-08-28T12:00:00+00:00"),
            ("regular", "2026-08-28T15:00:00+00:00"),
            ("afterhours", "2026-08-28T22:00:00+00:00"),
            ("overnight", "2026-08-29T02:00:00+00:00"),
        ]
        for index, (session, timestamp) in enumerate(cases):
            ticker = "AAA" if index % 2 == 0 else "BBB"
            engine = AlertEngine(self.store, self.send, threshold=1 + index / 10)
            self.assertEqual(
                await engine.process_message(
                    {
                        "type": "snapshot",
                        "prices": [price_point(ticker, 2, timestamp, session=session)],
                    },
                    self.universe,
                ),
                1,
            )

    async def test_stale_previous_cycle_and_negative_moves_are_ignored(self):
        points = [
            price_point("AAA", 2, "2026-08-28T12:00:00+00:00", stale=True),
            price_point("AAA", 2, "2026-08-28T07:59:00+00:00"),
            price_point("BBB", -2, "2026-08-28T12:00:00+00:00"),
        ]
        self.assertEqual(await self.process(*points), 0)
        self.assertEqual(self.sent, [])

    async def test_sqlite_deduplicates_after_worker_restart(self):
        point = price_point("AAA", 1.2, "2026-08-28T12:00:00+00:00")
        self.assertEqual(await self.process(point), 1)
        self.store.close()

        self.store = AlertStore(self.database)
        restarted = AlertEngine(self.store, self.send)
        self.assertEqual(
            await restarted.process_message(
                {"type": "snapshot", "prices": [point]},
                self.universe,
            ),
            0,
        )
        self.assertEqual(len(self.sent), 1)

    async def test_same_ticker_can_alert_in_the_next_cycle(self):
        self.assertEqual(
            await self.process(price_point("AAA", 1.2, "2026-08-28T12:00:00+00:00")),
            1,
        )
        next_universe = AlertUniverse("2026-08-29", self.universe.rows)
        self.assertEqual(
            await self.engine.process_message(
                {
                    "type": "snapshot",
                    "prices": [price_point("AAA", 1.3, "2026-08-29T12:00:00+00:00")],
                },
                next_universe,
            ),
            1,
        )

    async def test_definitive_rejection_releases_reservation_for_later_retry(self):
        attempts = 0

        async def reject_once(observation, threshold):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise DefinitiveSendError("missing permission")
            return "200"

        engine = AlertEngine(self.store, reject_once)
        await engine.process_message(
            {
                "type": "delta",
                "prices": [price_point("AAA", 1.2, "2026-08-28T12:00:00+00:00")],
            },
            self.universe,
        )
        await engine.process_message(
            {
                "type": "delta",
                "prices": [price_point("AAA", 0.8, "2026-08-28T12:00:01+00:00")],
            },
            self.universe,
        )
        self.assertEqual(
            await engine.process_message(
                {
                    "type": "delta",
                    "prices": [price_point("AAA", 1.3, "2026-08-28T12:00:02+00:00")],
                },
                self.universe,
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
