import asyncio
import unittest
from unittest.mock import Mock, patch
from scripts.live_price_adapter import DatabentoOptionFeed, option_symbol_from_mapping, parse_args
from scripts.option_contract_catalog import LiveOptionCatalog


class LiveCatalogTests(unittest.TestCase):
    def test_cached_pairs_survive_incremental_definition_updates(self):
        call, put = 'TSEM  260911C00200000', 'TSEM  260911P00200000'
        cached = {'contracts': {'TSEM': [{'call': call, 'put': put}]}}
        catalog = LiveOptionCatalog([{'ticker': 'TSEM'}], '2026-09-11', cached)
        self.assertEqual(catalog.revision, 0)
        self.assertEqual(len(catalog.payload()['contracts']['TSEM']), 1)
        catalog.update('TSEM  260911C00202500')
        self.assertEqual(len(catalog.payload()['contracts']['TSEM']), 1)
        catalog.update(put, deleted=True)
        self.assertEqual(catalog.payload()['contracts'], {})

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


class IndependentOptionStreamsTests(unittest.TestCase):
    def test_quote_connection_does_not_replay_definitions(self):
        feed = DatabentoOptionFeed('test', ['AAPL  261002C00340000'], Mock(), 0, asyncio.Event())
        with patch('scripts.live_price_adapter.IsolatedLive') as live:
            feed.connect()
        requests = [call.kwargs for call in live.return_value.subscribe.call_args_list]
        self.assertEqual([r['schema'] for r in requests], ['cbbo-1s'])
        self.assertNotIn('start', requests[0])
        self.assertEqual(feed.status_name, 'OPRA.PILLAR')

    def test_discovery_connection_has_separate_status_and_no_quotes(self):
        feed = DatabentoOptionFeed('test', [], Mock(), 0, asyncio.Event(),
                                   definition_parents=['AAPL.OPT'])
        with patch('scripts.live_price_adapter.IsolatedLive') as live:
            feed.connect()
        requests = [call.kwargs for call in live.return_value.subscribe.call_args_list]
        self.assertEqual([r['schema'] for r in requests], ['definition'])
        self.assertEqual(requests[0]['start'], 0)
        self.assertEqual(feed.status_name, 'OPRA.PILLAR.definitions')

    def test_cached_discovery_receives_only_new_definitions(self):
        feed = DatabentoOptionFeed('test', [], Mock(), 0, asyncio.Event(),
                                   definition_parents=['AAPL.OPT'], replay_definitions=False)
        with patch('scripts.live_price_adapter.IsolatedLive') as live:
            feed.connect()
        request = live.return_value.subscribe.call_args.kwargs
        self.assertEqual(request['schema'], 'definition')
        self.assertNotIn('start', request)


class StartupReplayTests(unittest.TestCase):
    def test_current_quotes_by_default_and_replay_is_opt_in(self):
        with patch('sys.argv', ['adapter']):
            self.assertEqual(parse_args().replay_minutes, 0)
        with patch('sys.argv', ['adapter', '--replay-minutes', '60']):
            self.assertEqual(parse_args().replay_minutes, 60)


class ProviderErrorRecordTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonfatal_error_uses_sdk_err_field_without_reconnecting(self):
        import databento as db
        from unittest.mock import AsyncMock
        stop = asyncio.Event()
        record = db.ErrorMsg(ts_event=0, err='slow reader notice',
                             code=db.ErrorCode.SKIPPED_RECORDS_AFTER_SLOW_READING, is_last=True)
        class Client:
            def __aiter__(self):
                async def records():
                    yield record
                    yield record
                    stop.set()
                return records()
            def stop(self):
                pass
        store = Mock()
        feed = DatabentoOptionFeed('test', ['AAPL  261002C00340000'], store, 0, stop)
        feed.connect_without_blocking_server = AsyncMock(return_value=Client())
        await feed.run()
        statuses = [call.args for call in store.set_feed_status.call_args_list]
        self.assertEqual(sum(s[1] == 'degraded' and s[2] == 'slow reader notice' for s in statuses), 2)
        self.assertFalse(any(s[1] == 'retrying' for s in statuses))
        feed.connect_without_blocking_server.assert_awaited_once()
