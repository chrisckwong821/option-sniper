import argparse
import tempfile
import unittest
from pathlib import Path

from aiohttp import WSMsgType
from aiohttp.client_exceptions import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from scripts.dashboard_state import build_mock_bundle
from scripts.live_price_adapter import create_app
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

        self.assertEqual((await self.client.get("/")).status, 200)
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
        await socket.close()

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
