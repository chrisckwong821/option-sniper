import argparse
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import WSMsgType
from aiohttp.client_exceptions import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from scripts.dashboard_state import build_mock_bundle
from scripts.live_price_adapter import STORE_KEY, create_app
from scripts.websocket_load_test import run as run_websocket_load_test
from tests.test_dashboard_state import sample_plan


class HttpIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(__file__).resolve().parents[1]
        self.args = argparse.Namespace(
            dashboard=root / "dashboard",
            cache=Path(self.tempdir.name) / "live-prices.json",
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
        state_response = await self.client.get("/api/state")
        self.assertEqual(state_response.status, 200)
        state = await state_response.json()
        self.assertEqual(len(state["snapshot"]["rows"]), 20)
        self.assertEqual(len(state["today"]["rows"]), 30)
        self.assertEqual(state_response.headers["Cache-Control"], "no-store")

        root_response = await self.client.get("/")
        self.assertEqual(root_response.status, 200)
        root_html = await root_response.text()
        self.assertIn("Expected &sigma;", root_html)
        self.assertNotIn(">1-day &sigma;<", root_html)
        self.assertEqual((await self.client.get("/app.js")).status, 200)
        self.assertEqual(
            (await self.client.get("/data/snapshot-state.json")).status,
            404,
        )

        health = await (await self.client.get("/api/health")).json()
        self.assertEqual(health["tracked_symbols"], 30)
        self.assertEqual(health["dashboard_state"]["data_date"], "2026-08-27")

        socket = await self.client.ws_connect("/ws/prices")
        message = await socket.receive(timeout=2)
        self.assertEqual(message.type, WSMsgType.TEXT)
        self.assertEqual(message.json()["type"], "snapshot")
        self.assertEqual(message.json()["schema_version"], "1.2")
        await socket.close()

    async def test_dirty_symbol_delta_is_shared_across_clients(self):
        args = argparse.Namespace(**vars(self.args))
        args.mock = False
        server = TestServer(create_app(args, build_mock_bundle(sample_plan())))
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
