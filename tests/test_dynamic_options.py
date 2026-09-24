import asyncio
import unittest
from unittest.mock import Mock
from scripts.live_price_adapter import DatabentoOptionFeed, option_symbol_from_mapping
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


class OptionMappingTests(unittest.TestCase):
    def test_numeric_output_resolves_input_occ_symbol(self):
        symbol = 'AAPL  261002C00340000'
        record = Mock(stype_in_symbol=symbol, stype_out_symbol='123456')
        self.assertEqual(option_symbol_from_mapping(record), symbol)

    def test_parent_input_resolves_output_occ_symbol(self):
        symbol = 'TSEM  261002P00200000'
        record = Mock(stype_in_symbol='TSEM.OPT', stype_out_symbol=symbol)
        self.assertEqual(option_symbol_from_mapping(record), symbol)

    def test_empty_output_and_null_padding(self):
        record = Mock(stype_in_symbol='AAPL  261002C00340000\x00', stype_out_symbol='\x00')
        self.assertEqual(option_symbol_from_mapping(record), 'AAPL  261002C00340000')

    def test_invalid_mapping_is_not_used_as_contract(self):
        for input_symbol, output in [('AAPL.OPT', '123456'), ('', ''), ('123456', '123456')]:
            with self.subTest(input=input_symbol, output=output):
                self.assertIsNone(option_symbol_from_mapping(Mock(
                    stype_in_symbol=input_symbol, stype_out_symbol=output)))
