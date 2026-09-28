import asyncio
import base64
import unittest
from unittest.mock import Mock

import databento as db
from aiohttp import web, ClientSession
from scripts.live_ingestion import IngestionBridge, IsolatedLive
from scripts.live_ingestion_worker import PendingRecords
from scripts.app_supervisor import monitor


class IngestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_hung_worker_is_killed_http_stays_responsive_and_replacement_delivers(self):
        bridge = IngestionBridge(heartbeat_timeout=.5, worker_module='tests.fake_ingestion_worker')
        app = web.Application()
        async def handler(request):
            return web.Response(text='ok')
        app.router.add_get('/', handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        stalled = IsolatedLive(key='stall')
        bridge.register(stalled)
        try:
            async with ClientSession() as http:
                for _ in range(20):
                    async with http.get(f'http://127.0.0.1:{port}/', timeout=.2) as response:
                        self.assertEqual(await response.text(), 'ok')
                    await asyncio.sleep(.05)
            self.assertGreaterEqual(bridge.restarts, 1)
            self.assertIsNotNone(stalled.error)
            with self.assertRaises(RuntimeError):
                await stalled.__anext__()
            record = db.ErrorMsg(1, 'replacement delivered')
            replacement = IsolatedLive(key='echo', fixture_data=base64.b64encode(bytes(record)).decode())
            bridge.register(replacement)
            received = await asyncio.wait_for(replacement.__anext__(), 5)
            self.assertEqual(received.err, 'replacement delivered')
        finally:
            await bridge.close()
            await runner.cleanup()
        self.assertIsNotNone(bridge.process.returncode)

    async def test_proxy_control_and_consumer_queues_are_bounded(self):
        bridge = IngestionBridge()
        for i in range(128):
            bridge.send({'op': 'close', 'id': str(i)})
        with self.assertRaisesRegex(RuntimeError, 'queue full'):
            bridge.send({'op': 'close', 'id': 'overflow'})
        client = IsolatedLive(key='fake')
        for _ in range(client.queue.maxsize):
            client.queue.put_nowait(b'x')
        client.fail('consumer overflow')
        with self.assertRaisesRegex(RuntimeError, 'consumer overflow'):
            await client.__anext__()
        self.assertEqual(client.queue.qsize(), 1)


class PendingTests(unittest.TestCase):
    def test_control_overflow_is_bounded_and_visible(self):
        pending = PendingRecords(limit=2)
        for i in range(100):
            pending.add(db.ErrorMsg(i, 'notice'))
        self.assertTrue(pending.overflow)
        self.assertEqual(len(pending.control), 3)

    def test_quote_coalescing_keeps_latest_and_control_before_quotes(self):
        from databento_dbn import DBNDecoder
        pending = PendingRecords()
        for ts in range(20):
            pending.add(db.OHLCVMsg(db.RType.OHLCV_1S, 1, 123, ts,
                                   100, 100, 100, 100 + ts, 1))
        pending.add(db.ErrorMsg(1, 'control'))
        records = DBNDecoder(has_metadata=False).write_and_decode(pending.take())
        self.assertEqual(len(records), 2)
        self.assertIsInstance(records[0], db.ErrorMsg)
        self.assertEqual(records[1].close, 119)

    def test_binary_roundtrip_preserves_mapping_and_quote_types(self):
        from databento_dbn import DBNDecoder
        pending = PendingRecords()
        record = db.SymbolMappingMsg(publisher_id=1, instrument_id=123, ts_event=1, stype_in=db.SType.RAW_SYMBOL,
            stype_in_symbol='AAPL  261002C00340000', stype_out=db.SType.INSTRUMENT_ID,
            stype_out_symbol='123', start_ts=0, end_ts=0)
        pending.add(record)
        result = DBNDecoder(has_metadata=False).write_and_decode(pending.take())[0]
        self.assertEqual(result.instrument_id, 123)
        self.assertEqual(result.stype_in_symbol, 'AAPL  261002C00340000')


class WatchdogTests(unittest.TestCase):
    def test_only_consecutive_liveness_failures_trigger_recovery(self):
        child = Mock()
        child.poll.return_value = None
        probe = Mock(side_effect=[False, False, True, False, False, False])
        now = [0]
        def sleep(seconds):
            now[0] += seconds
        result = monitor(child, 'unused', startup_grace=20, interval=10,
                         probe=probe, clock=lambda: now[0], sleep=sleep)
        self.assertEqual(result, 1)
        self.assertEqual(probe.call_count, 6)
        self.assertEqual(now[0], 70)
