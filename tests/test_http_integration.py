import argparse
import asyncio
import math
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from aiohttp import WSMsgType
from aiohttp.client_exceptions import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from scripts.dashboard_state import atomic_write_json, build_mock_bundle, build_refresh_plan
from scripts.discord_zscore_alert import build_universe
from scripts.live_price_adapter import STORE_KEY, create_app
from scripts.expected_sigma import sigma_plan
from scripts.websocket_load_test import run as run_websocket_load_test
from tests.test_dashboard_state import sample_plan


class HttpIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(__file__).resolve().parents[1]
        self.args = argparse.Namespace(
            dashboard=root / "dashboard",
            cache=Path(self.tempdir.name) / "live-prices.json",
            iv_cache=Path(self.tempdir.name) / "live-iv-expansion.json",
            mock=True,
            allowed_origin=[],
            day_dataset="EQUS.MINI",
            overnight_dataset="OCEA.MEMOIR",
            replay_minutes=1,
            disable_overnight=True,
            broadcast_interval=0.01,
            status_interval=0.05,
            send_timeout=0.5,
            mock_symbols=30,
        )
        bundle = build_mock_bundle(sample_plan())
        self.client = TestClient(TestServer(create_app(self.args, bundle)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tempdir.cleanup()

    async def test_state_assets_health_and_websocket(self):
        state_response = await self.client.get(
            "/api/state", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(state_response.status, 200)
        self.assertEqual(state_response.headers["Content-Encoding"], "gzip")
        self.assertIn("Accept-Encoding", state_response.headers["Vary"])
        state = await state_response.json()
        self.assertEqual(len(state["snapshot"]["rows"]), 20)
        self.assertEqual(len(state["today"]["rows"]), 30)
        self.assertEqual(state_response.headers["Cache-Control"], "no-store")
        unchanged = await self.client.get("/api/state", headers={
            "If-None-Match": state_response.headers["ETag"],
        })
        self.assertEqual(unchanged.status, 304)
        self.assertEqual(await unchanged.read(), b"")

        root_response = await self.client.get("/")
        self.assertEqual(root_response.status, 200)
        root_html = await root_response.text()
        self.assertIn("Expected &sigma;", root_html)
        self.assertNotIn(">1-day &sigma;<", root_html)
        self.assertEqual((await self.client.get("/app.js")).status, 200)
        self.assertEqual((await self.client.get("/state-refresh.js")).status, 200)
        self.assertEqual(
            (await self.client.get("/data/snapshot-state.json")).status,
            404,
        )

        health_response = await self.client.get(
            "/api/health", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(health_response.headers["Content-Encoding"], "gzip")
        self.assertIn("Accept-Encoding", health_response.headers["Vary"])
        health = await health_response.json()
        self.assertEqual(health["tracked_symbols"], 30)
        self.assertEqual(health["dashboard_state"]["data_date"], "2026-08-27")

        socket = await self.client.ws_connect("/ws/prices")
        message = await socket.receive(timeout=2)
        self.assertEqual(message.type, WSMsgType.TEXT)
        self.assertEqual(message.json()["type"], "snapshot")
        self.assertEqual(message.json()["schema_version"], "1.2")
        await socket.close()

    async def test_weekly_universe_limits_api_price_store_and_iv_roots(self):
        bundle = build_mock_bundle(sample_plan())
        selected = bundle["today"]["rows"][0]
        root = (selected.get("iv_chain") or {}).get("occ_root") or selected["ticker"]
        report_path = self.args.cache.parent / "occ-weekly-options.json"
        atomic_write_json(report_path, {"status": "available", "symbols": [root],
                                       "as_of": "2026-09-09", "expiration": "2026-10-30"})
        app = create_app(self.args, bundle)
        self.assertEqual(app[STORE_KEY].symbols, frozenset([selected["ticker"]]))
        self.assertEqual(set(app[STORE_KEY].iv_tracker.pending_roots), {selected["ticker"]})
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            state = await (await client.get("/api/state")).json()
            self.assertEqual(state["today"]["valid_iv_coverage"], 1)
            self.assertEqual([r["ticker"] for r in state["today"]["rows"]], [selected["ticker"]])
            self.assertTrue(all(r["ticker"] == selected["ticker"] for r in state["snapshot"]["rows"]))
            # A changed file cannot silently diverge API coverage from active subscriptions.
            atomic_write_json(report_path, {"status": "unavailable", "symbols": []})
            state_again = await (await client.get("/api/state")).json()
            self.assertEqual(state_again["weekly_options"], state["weekly_options"])
            health = await (await client.get("/api/health")).json()
            self.assertEqual(health["tracked_symbols"], 1)
            excluded = bundle["today"]["rows"][1]["ticker"]
            self.assertFalse(app[STORE_KEY].update_price(excluded, 10, datetime.now(UTC), "TEST"))
        finally:
            await client.close()

    async def test_early_gainer_refresh_keeps_today_and_alert_universe(self):
        original = await (await self.client.get("/api/state")).json()
        prior_response = await self.client.get("/api/state")
        saved = build_mock_bundle(build_refresh_plan(datetime(2026, 8, 29, 0, 20, tzinfo=UTC)))
        saved["snapshot"]["rows"][0]["ticker"] = "NEWGAINER"
        saved_path = Path(self.tempdir.name) / "yesterday-state.json"
        atomic_write_json(saved_path, saved)
        args = argparse.Namespace(**vars(self.args), yesterday_state_cache=saved_path)
        app = create_app(args, original)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get("/api/state", headers={
                "If-None-Match": prior_response.headers["ETag"],
            })
            self.assertEqual(response.status, 200)
            published = await response.json()
            self.assertEqual(published["snapshot"]["data_date"], "2026-08-28")
            self.assertEqual(published["today"], original["today"])
            self.assertEqual(published["refresh_plan"], original["refresh_plan"])
            self.assertEqual(build_universe(published), build_universe(original))
            self.assertIn("NEWGAINER", app[STORE_KEY].symbols)
            self.assertTrue(set(build_universe(original).rows) <= app[STORE_KEY].symbols)
        finally:
            await client.close()

    async def test_late_sigma_changes_etag_without_restarting_server(self):
        # The already-started mock feeds remain isolated from provider APIs.
        self.args.mock = False
        plan = sigma_plan(datetime(2026, 9, 9, 8, 12, tzinfo=UTC))
        with patch('scripts.expected_sigma.sigma_plan', return_value=plan):
            before = await self.client.get('/api/state')
            pending = await before.json()
            self.assertEqual(pending['expected_sigma']['status'], 'awaiting_previous_close')
            rows = {row['ticker']: {'sigma_1d_pct': 2,
                                   'annualized_implied_vol_pct': 2 / math.sqrt(plan['year_fraction'])}
                    for row in pending['today']['rows']}
            atomic_write_json(Path(self.tempdir.name) / 'expected-sigma-min2dte-v2' / '2026-09-09.json',
                              {'plan': plan, 'rows': rows})
            after = await self.client.get('/api/state', headers={'If-None-Match': before.headers['ETag']})
            self.assertEqual(after.status, 200)
            ready = await after.json()
            self.assertEqual(ready['expected_sigma']['status'], 'fresh')
            for view in ('today', 'snapshot'):
                self.assertTrue(all(r['sigma_1d_pct'] == 2 for r in ready[view]['rows']))
            unchanged = await self.client.get('/api/state', headers={'If-None-Match': after.headers['ETag']})
            self.assertEqual(unchanged.status, 304)

    async def test_slow_sigma_read_does_not_block_health_or_duplicate_readers(self):
        self.args.mock = False
        entered, release = threading.Event(), threading.Event()
        calls = []
        def slow_apply(bundle):
            calls.append(True)
            entered.set()
            release.wait(3)
            return bundle
        with patch('scripts.expected_sigma.ExpectedSigma.apply', side_effect=slow_apply):
            first = asyncio.ensure_future(self.client.get('/api/state'))
            second = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                second = asyncio.ensure_future(self.client.get('/api/state'))
                health = await asyncio.wait_for(self.client.get('/api/health'), 1)
                self.assertEqual(health.status, 200)
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
                await first
                if second is not None:
                    await second

    async def test_dirty_symbol_delta_is_shared_across_clients(self):
        args = argparse.Namespace(**vars(self.args))
        args.mock = False
        bundle = build_mock_bundle(sample_plan())
        atomic_write_json(args.cache.parent / "occ-weekly-options.json", {
            "status": "available", "as_of": "2026-09-09", "expiration": "2026-10-30",
            "symbols": [(r.get("iv_chain") or {}).get("occ_root") or r["ticker"]
                        for r in bundle["today"]["rows"]],
        })
        server = TestServer(create_app(args, bundle))
        client = TestClient(server)
        await client.start_server()
        try:
            first = await client.ws_connect("/ws/prices")
            second = await client.ws_connect("/ws/prices")
            self.assertEqual((await first.receive(timeout=2)).json()["type"], "snapshot")
            self.assertEqual((await second.receive(timeout=2)).json()["type"], "snapshot")

            store = server.app[STORE_KEY]
            self.assertTrue(store.update_price(
                "DEMO01",
                51.25,
                datetime(2026, 8, 28, 13, 30, tzinfo=UTC),
                "TEST.LIVE",
                100,
            ))

            async def receive_delta(socket):
                for _ in range(4):
                    message = await socket.receive(timeout=2)
                    payload = message.json()
                    if payload["type"] == "delta":
                        return message.data, payload
                self.fail("WebSocket did not receive a delta")

            first_text, first_delta = await receive_delta(first)
            second_text, second_delta = await receive_delta(second)
            self.assertEqual(first_text, second_text)
            self.assertEqual(first_delta["sequence"], 1)
            self.assertEqual(
                [point["ticker"] for point in first_delta["prices"]],
                ["DEMO01"],
            )
            self.assertEqual(store.stream_metrics()["delta_broadcasts"], 1)
            self.assertEqual(store.stream_metrics()["last_batch_symbols"], 1)
            await first.close()
            await second.close()
        finally:
            await client.close()

    async def test_load_harness_smoke_run(self):
        load_args = argparse.Namespace(
            url=str(self.client.make_url("/ws/prices")).replace("http://", "ws://", 1),
            health_url=str(self.client.make_url("/api/health")),
            viewers=10,
            duration=1.2,
            ramp_seconds=0,
            connect_timeout=5,
            max_p95_latency_ms=500,
            min_delivery_ratio=0.5,
            origin=None,
            no_compression=False,
        )
        result = await run_websocket_load_test(load_args)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["connected_viewers"], 10)
        self.assertEqual(result["sequence_gaps"], 0)

    async def test_private_paths_and_legacy_price_api_are_not_routed(self):
        for path in (
            "/api/prices",
            "/data/snapshot-state.json",
            "/.cache/sigma-tape/dashboard-state.json",
            "/README.md",
        ):
            with self.subTest(path=path):
                self.assertEqual((await self.client.get(path)).status, 404)

    async def test_websocket_origin_allowlist_rejects_other_sites(self):
        self.args.allowed_origin = ["https://sigma.example"]
        client = TestClient(TestServer(create_app(
            self.args,
            build_mock_bundle(sample_plan()),
        )))
        await client.start_server()
        try:
            with self.assertRaises(WSServerHandshakeError) as context:
                await client.ws_connect(
                    "/ws/prices",
                    headers={"Origin": "https://evil.example"},
                )
            self.assertEqual(context.exception.status, 403)

            socket = await client.ws_connect(
                "/ws/prices",
                headers={"Origin": "https://sigma.example"},
            )
            self.assertEqual((await socket.receive(timeout=2)).type, WSMsgType.TEXT)
            await socket.close()
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
