import asyncio
import unittest
from unittest.mock import Mock
from scripts.live_price_adapter import DatabentoOptionFeed
from scripts.option_contract_catalog import LiveOptionCatalog


class LiveCatalogTests(unittest.TestCase):
    def test_retains_only_listed_pairs_for_requested_expiry_and_roots(self):
        catalog = LiveOptionCatalog([{'ticker': 'TSEM'}], '2026-09-11')
        call, put = 'TSEM  260911C00200000', 'TSEM  260911P00200000'
        for symbol in [call, put, call, 'TSEM  260918C00200000', 'BKNG  260911C00200000']:
            catalog.update(symbol)
        self.assertEqual(catalog.revision, 2)
        self.assertEqual(len(catalog.payload()['contracts']['TSEM']), 1)
        catalog.update(put, deleted=True)
        self.assertEqual(catalog.payload()['contracts'], {})


class ReplanTests(unittest.IsolatedAsyncioTestCase):
    async def test_removal_rotates_to_exact_set(self):
        stop = asyncio.Event()
        feed = DatabentoOptionFeed('test', ['old', 'keep'], Mock(), 0, stop,
                                   symbol_provider=lambda: ['keep', 'new'],
                                   replan_interval_seconds=.001)
        feed.client = Mock()
        await feed.refresh_subscriptions()
        self.assertEqual(feed.symbols, {'keep', 'new'})
        feed.client.terminate.assert_called_once()
        self.assertEqual(feed.last_plan['removed'], 1)
        self.assertEqual(feed.last_plan['added'], 1)

    async def test_addition_does_not_restart_session(self):
        stop = asyncio.Event()
        feed = DatabentoOptionFeed('test', ['keep'], Mock(), 0, stop,
                                   symbol_provider=lambda: ['keep', 'new'],
                                   replan_interval_seconds=.001)
        feed.client = Mock()
        feed.client.subscribe.side_effect = lambda **kw: stop.set()
        await feed.refresh_subscriptions()
        feed.client.terminate.assert_not_called()
        self.assertEqual(feed.symbols, {'keep', 'new'})
        self.assertNotIn('start', feed.client.subscribe.call_args.kwargs)

    async def test_empty_plan_preserves_working_subscription(self):
        stop = asyncio.Event()
        def provider():
            stop.set()
            return []
        feed = DatabentoOptionFeed('test', ['keep'], Mock(), 0, stop,
                                   symbol_provider=provider, replan_interval_seconds=.001)
        feed.client = Mock()
        await feed.refresh_subscriptions()
        self.assertEqual(feed.symbols, {'keep'})
        feed.client.terminate.assert_not_called()
