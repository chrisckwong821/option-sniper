import copy
import math
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from scripts.dashboard_state import atomic_write_json
from scripts.expected_sigma import ExpectedSigma, sigma_plan

def at(value):
    return datetime.fromisoformat(value)

def bundle():
    row = dict(ticker='AAA', sigma_1d_pct=9, annualized_implied_vol_pct=60,
               log_return_pct=6, z_score=2, gain_pct=6.18, last_price=106,
               prior_session_gain_pct=6.18)
    return {view: dict(rows=[copy.deepcopy(row)], data_date='2026-09-08',
                       anchor_date='2026-09-08', iv_snapshot_time='2026-09-03T20:00:00+00:00')
            for view in ('today', 'snapshot')}

class SigmaTests(unittest.TestCase):
    def test_thursday_friday_roll_to_next_weekly(self):
        for session in ('2026-09-10', '2026-09-11'):
            plan = sigma_plan(at(session + 'T10:00:00-04:00'))
            self.assertEqual(plan['option_expiry'], '2026-09-18')
        self.assertEqual(sigma_plan(at('2026-09-09T10:00:00-04:00'))['option_expiry'], '2026-09-11')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sigma = ExpectedSigma(Path(self.tmp.name))
    def tearDown(self):
        self.tmp.cleanup()
    def save(self, when, value=2):
        plan = sigma_plan(at(when))
        atomic_write_json(self.sigma.root / (plan['effective_session'] + '.json'), {
            'plan': plan, 'rows': {'AAA': dict(sigma_1d_pct=value, annualized_implied_vol_pct=value / math.sqrt(plan['year_fraction']),
                                             iv_reliability='high')}})
    def test_premarket_boundary_and_holiday_horizon(self):
        before = sigma_plan(at('2026-09-09T03:59:59-04:00'))
        after = sigma_plan(at('2026-09-09T04:00:00-04:00'))
        self.assertEqual(before['iv_session'], '2026-09-04')
        self.assertAlmostEqual(before['year_fraction'], 4/365)
        self.assertEqual(after['iv_session'], '2026-09-08')
        self.assertAlmostEqual(after['year_fraction'], 1/365)
    def test_afterhours_does_not_roll(self):
        self.assertEqual(sigma_plan(at('2026-09-09T18:00:00-04:00'))['iv_session'], '2026-09-08')
    def test_weekend_and_holiday_do_not_activate(self):
        for date in ['2026-09-05', '2026-09-06', '2026-09-07']:
            self.assertEqual(sigma_plan(at(date+'T12:00:00-04:00'))['effective_session'], '2026-09-04')
    def test_early_close_horizon(self):
        plan = sigma_plan(at('2026-11-30T04:00:00-05:00'))
        self.assertEqual(plan['iv_session'], '2026-11-27')
        self.assertAlmostEqual(plan['year_fraction'], 75/24/365)
    def test_prepared_snapshot_is_not_activated_early(self):
        self.save('2026-09-08T04:00:00-04:00', 3)
        self.save('2026-09-09T04:00:00-04:00', 2)
        result = self.sigma.apply(bundle(), at('2026-09-09T03:59:00-04:00'))
        self.assertEqual(result['today']['rows'][0]['sigma_1d_pct'], 3)
    def test_late_publication_updates_both_views(self):
        when = at('2026-09-09T04:12:00-04:00')
        source = bundle()
        self.assertEqual(self.sigma.apply(source, when)['expected_sigma']['status'], 'awaiting_previous_close')
        self.save(when.isoformat())
        result = self.sigma.apply(source, when)
        for view in ('today', 'snapshot'):
            row = result[view]['rows'][0]
            self.assertEqual(row['sigma_1d_pct'], 2)
            self.assertEqual(row['z_score'], 3)
            self.assertEqual(row['last_price'], 106)
            self.assertEqual(row['expected_sigma_status'], 'fresh')
        self.assertEqual(source['today']['rows'][0]['sigma_1d_pct'], 9)
    def test_pending_uses_previous_shared_snapshot_after_restart(self):
        self.save('2026-09-08T04:00:00-04:00', 3)
        self.sigma = ExpectedSigma(Path(self.tmp.name))
        result = self.sigma.apply(bundle(), at('2026-09-09T04:12:00-04:00'))
        for view in ('today', 'snapshot'):
            self.assertEqual(result[view]['rows'][0]['sigma_1d_pct'], 3)
            self.assertEqual(result[view]['rows'][0]['expected_sigma_status'], 'awaiting_previous_close')
    def test_wrong_provenance_rejected(self):
        when = at('2026-09-09T04:12:00-04:00')
        plan = sigma_plan(when)
        plan['iv_session'] = '2026-09-03'
        atomic_write_json(self.sigma.root/'2026-09-09.json', {'plan': plan, 'rows': {'AAA': {'sigma_1d_pct': 2}}})
        self.assertEqual(self.sigma.apply(bundle(), when)['expected_sigma']['status'], 'awaiting_previous_close')
    def test_union_and_close_window(self):
        source = bundle()
        source['snapshot']['rows'].append({**source['snapshot']['rows'][0], 'ticker': 'BBB'})
        quotes = [dict(ts_recv='2026-09-08T19:59:59+00:00', symbol='AAA', bid_px_00=1, ask_px_00=2),
                  dict(ts_recv='2026-09-08T20:00:02+00:00', symbol='AAA', bid_px_00=8, ask_px_00=9)]
        with patch('scripts.expected_sigma.resolve_live_option_api_key', return_value='test'), \
             patch('scripts.expected_sigma.replay_quotes', return_value=quotes) as replay, \
             patch('scripts.expected_sigma.prepare_option_pairs', return_value=({}, {})) as pairs, \
             patch('scripts.expected_sigma.estimate_option_iv', return_value={'annualized_implied_vol_pct':40,'iv_reliability':'high'}):
            self.sigma.refresh(source, at('2026-09-09T04:12:00-04:00'))
        self.assertEqual(replay.call_args.args[1], ['AAA', 'BBB'])
        self.assertEqual(len(pairs.call_args.args[0]), 1)
        result = self.sigma.apply(source, at('2026-09-09T04:13:00-04:00'))
        self.assertAlmostEqual(result['today']['rows'][0]['sigma_1d_pct'], 40 * math.sqrt(1/365))
        self.assertEqual(result['expected_sigma']['status'], 'fresh')

    def test_prepare_friday_before_holiday_uses_live_replay(self):
        from scripts.expected_sigma import next_activation
        now = at('2026-09-04T16:05:00-04:00')
        activation = next_activation(now)
        self.assertEqual(activation.isoformat(), '2026-09-08T04:00:00-04:00')
        source = bundle()
        for view in ('today', 'snapshot'):
            source[view]['data_date'] = '2026-09-04'
        quotes = [dict(ts_recv='2026-09-04T19:59:59+00:00', symbol='AAA', bid_px_00=1, ask_px_00=2)]
        with patch('scripts.expected_sigma.resolve_live_option_api_key', return_value='test'), \
             patch('scripts.expected_sigma.replay_quotes', return_value=quotes) as replay, \
             patch('scripts.expected_sigma.prepare_option_pairs', return_value=({}, {})), \
             patch('scripts.expected_sigma.estimate_option_iv', return_value={'annualized_implied_vol_pct':40,'iv_reliability':'high'}):
            self.sigma.refresh(source, now, effective_at=activation)
        replay.assert_called_once()
        self.assertTrue((self.sigma.root/'2026-09-08.json').exists())
        self.assertEqual(self.sigma.apply(source, now)['expected_sigma']['effective_session'], '2026-09-04')

    def test_replay_failure_uses_historical_fallback(self):
        quotes = [dict(ts_recv='2026-09-08T19:59:59+00:00', symbol='AAA', bid_px_00=1, ask_px_00=2)]
        with patch('scripts.expected_sigma.resolve_live_option_api_key', return_value='test'), \
             patch('scripts.expected_sigma.resolve_historical_api_keys', return_value=(None, 'history')), \
             patch('scripts.expected_sigma.replay_quotes', side_effect=RuntimeError('unavailable')), \
             patch('scripts.expected_sigma.historical_quotes', return_value=quotes) as historical, \
             patch('scripts.expected_sigma.prepare_option_pairs', return_value=({}, {})), \
             patch('scripts.expected_sigma.estimate_option_iv', return_value=None):
            self.sigma.refresh(bundle(), at('2026-09-09T04:12:00-04:00'))
        self.assertEqual(historical.call_count, 2)
        self.assertEqual(historical.call_args_list[1].kwargs['window_minutes'], 15)

    def test_unpaired_close_uses_latest_coherent_fallback_minute(self):
        from scripts.iv_expansion import format_occ_symbol
        call = format_occ_symbol('AAA', '2026-09-11', 'C', 105)
        put = format_occ_symbol('AAA', '2026-09-11', 'P', 105)
        close_quotes = [
            dict(ts_recv='2026-09-08T20:00:00+00:00', symbol=call,
                 bid_px_00=float('nan'), ask_px_00=2.2),
            dict(ts_recv='2026-09-08T20:00:00+00:00', symbol=put,
                 bid_px_00=1.0, ask_px_00=1.2),
        ]
        earlier_quotes = [
            dict(ts_recv='2026-09-08T19:54:00.000Z', symbol=call,
                 bid_px_00=2.0, ask_px_00=2.2),
            dict(ts_recv='2026-09-08T19:54:00.000Z', symbol=put,
                 bid_px_00=1.0, ask_px_00=1.2),
        ]
        with patch('scripts.expected_sigma.resolve_live_option_api_key', return_value='live'), \
             patch('scripts.expected_sigma.resolve_historical_api_keys', return_value=(None, 'history')), \
             patch('scripts.expected_sigma.replay_quotes', return_value=close_quotes), \
             patch('scripts.expected_sigma.historical_quotes', return_value=earlier_quotes) as historical:
            self.sigma.refresh(bundle(), at('2026-09-09T04:12:00-04:00'))
        row = self.sigma.apply(bundle(), at('2026-09-09T04:13:00-04:00'))['today']['rows'][0]
        self.assertEqual(row['expected_sigma_status'], 'fresh')
        saved = __import__('scripts.expected_sigma', fromlist=['read_json']).read_json(
            self.sigma.root / '2026-09-09.json'
        )['rows']['AAA']
        self.assertEqual(saved['iv_quote_time'], '2026-09-08T19:54:00+00:00')
        self.assertEqual(saved['iv_quote_age_minutes'], 6)
        self.assertEqual(historical.call_args.kwargs['window_minutes'], 15)

    def test_quotes_persist_even_when_spot_is_not_ready(self):
        quotes = [dict(ts_recv='2026-09-08T19:59:59+00:00', symbol='AAA', bid_px_00=1, ask_px_00=2)]
        source = bundle()
        for view in ('today', 'snapshot'):
            source[view]['data_date'] = '2026-09-04'
        with patch('scripts.expected_sigma.resolve_live_option_api_key', return_value='test'), \
             patch('scripts.expected_sigma.resolve_historical_api_keys', return_value=(None, None)), \
             patch('scripts.expected_sigma.replay_quotes', return_value=quotes):
            with self.assertRaisesRegex(RuntimeError, 'closing stock prices'):
                self.sigma.refresh(source, at('2026-09-09T04:12:00-04:00'))
        self.assertTrue((self.sigma.root/'close-2026-09-08.json').exists())
        self.assertFalse((self.sigma.root/'2026-09-09.json').exists())

    def test_corrupt_sigma_denominator_is_not_fresh(self):
        when = at('2026-09-09T04:12:00-04:00')
        plan = sigma_plan(when)
        atomic_write_json(self.sigma.root/'2026-09-09.json', {'plan': plan, 'rows': {
            'AAA': {'sigma_1d_pct': -2, 'annualized_implied_vol_pct':40}}})
        self.assertEqual(self.sigma.apply(bundle(), when)['expected_sigma']['status'], 'awaiting_previous_close')

    def test_saved_gainers_without_log_return_recalculate_from_saved_gain(self):
        when = at('2026-09-09T04:12:00-04:00')
        source = bundle()
        del source['snapshot']['rows'][0]['log_return_pct']
        self.save(when.isoformat())
        result = self.sigma.apply(source, when)
        self.assertAlmostEqual(result['snapshot']['rows'][0]['z_score'], 100 * math.log1p(.0618) / 2)

    def test_replay_timeout_never_publishes_partial_quotes(self):
        from unittest.mock import MagicMock
        from scripts.expected_sigma import replay_quotes
        client = MagicMock()
        with patch('scripts.expected_sigma.db.Live', return_value=client):
            with self.assertRaisesRegex(RuntimeError, 'did not finish'):
                replay_quotes('test', ['AAA'], at('2026-09-08T20:00:00+00:00'))
        client.terminate.assert_called_once()

    def test_historical_cost_guard_prevents_download(self):
        from unittest.mock import MagicMock
        from scripts.expected_sigma import historical_quotes
        client = MagicMock()
        client.metadata.get_cost.return_value = 6
        with patch('scripts.expected_sigma.db.Historical', return_value=client):
            with self.assertRaisesRegex(RuntimeError, 'cost limit'):
                historical_quotes('test', ['AAA'], at('2026-09-08T20:00:00+00:00'), 5)
        client.timeseries.get_range.assert_not_called()

    def test_historical_cost_guard_spans_primary_and_fallback(self):
        import pandas as pd
        from unittest.mock import MagicMock
        from scripts.expected_sigma import historical_quotes
        client = MagicMock()
        client.metadata.get_cost.return_value = 3
        client.timeseries.get_range.return_value.to_df.return_value = pd.DataFrame()
        costs = []
        with patch('scripts.expected_sigma.db.Historical', return_value=client):
            historical_quotes('test', ['AAA'], at('2026-09-08T20:00:00+00:00'), 5,
                              cost_tracker=costs)
            with self.assertRaisesRegex(RuntimeError, 'cost limit'):
                historical_quotes('test', ['BBB'], at('2026-09-08T20:00:00+00:00'), 5,
                                  window_minutes=15, cost_tracker=costs)
        self.assertEqual(costs, [3])
        self.assertEqual(client.timeseries.get_range.call_count, 1)

    def test_known_contracts_use_bounded_minute_requests(self):
        import pandas as pd
        from unittest.mock import MagicMock
        from scripts.expected_sigma import historical_quotes
        client = MagicMock()
        client.metadata.get_cost.return_value = 0
        client.timeseries.get_range.return_value.to_df.return_value = pd.DataFrame()
        symbol = 'AAPL  260911C00200000'
        with patch('scripts.expected_sigma.db.Historical', return_value=client):
            self.assertEqual(historical_quotes('test', ['AAPL'], at('2026-09-04T20:00:00+00:00'), 5,
                                              contracts={'AAPL': [symbol]}), [])
        request = client.timeseries.get_range.call_args.kwargs
        self.assertEqual(request['symbols'], [symbol])
        self.assertEqual(request['stype_in'], 'raw_symbol')
        self.assertEqual(request['schema'], 'cbbo-1m')

    def test_historical_fallback_request_uses_requested_window(self):
        import pandas as pd
        from unittest.mock import MagicMock
        from scripts.expected_sigma import historical_quotes
        client = MagicMock()
        client.metadata.get_cost.return_value = 0
        client.timeseries.get_range.return_value.to_df.return_value = pd.DataFrame()
        with patch('scripts.expected_sigma.db.Historical', return_value=client):
            historical_quotes('test', ['AAA'], at('2026-09-08T20:00:00+00:00'), 5,
                              window_minutes=15)
        request = client.timeseries.get_range.call_args.kwargs
        self.assertEqual(request['start'], at('2026-09-08T19:45:00+00:00'))

    def test_post_close_record_is_not_replay_completion(self):
        from unittest.mock import MagicMock
        from types import SimpleNamespace
        from scripts.expected_sigma import replay_quotes
        client = MagicMock()
        close = at('2026-09-08T20:00:00+00:00')
        def emit():
            callback = client.add_callback.call_args.args[0]
            callback(SimpleNamespace(ts_recv=int(close.timestamp()*1e9)+2000000000,
                                     pretty_bid_px_00=1, pretty_ask_px_00=2))
        client.start.side_effect = emit
        with patch('scripts.expected_sigma.db.Live', return_value=client):
            with self.assertRaisesRegex(RuntimeError, 'did not finish'):
                replay_quotes('test', ['AAA'], close)
        client.stop.assert_not_called()

    def test_both_lists_receive_verified_active_session_return_anchor(self):
        when = at('2026-09-09T04:12:00-04:00')
        source = bundle()
        atomic_write_json(self.sigma.root/'close-2026-09-08.json', {'spots': {'AAA': 110}})
        result = self.sigma.apply(source, when)
        for view in ('today', 'snapshot'):
            row = result[view]['rows'][0]
            self.assertEqual(row['return_anchor_price'], 110)
            self.assertEqual(row['live_anchor_price'], 110)
            self.assertEqual(row['return_anchor_date'], '2026-09-08')
            self.assertEqual(row['gain_pct'], source[view]['rows'][0]['gain_pct'])
        early = self.sigma.apply(source, at('2026-09-09T03:59:00-04:00'))
        self.assertNotIn('return_anchor_price', early['today']['rows'][0])


class SharedSigmaTests(unittest.TestCase):
    setUp = SigmaTests.setUp
    tearDown = SigmaTests.tearDown
    save = SigmaTests.save
    def configure_shared(self):
        from scripts.shared_cache import SharedCacheError
        objects = {}
        def get(key, path):
            if key not in objects:
                return False
            atomic_write_json(path, objects[key])
            return True
        def update(key, callback):
            objects[key] = callback(copy.deepcopy(objects.get(key, {})))
            return objects[key]
        from unittest.mock import Mock
        self.sigma.shared = Mock(get=get, update_json=update)
        return objects

    def test_second_instance_restores_sigma_and_anchor_without_databento(self):
        objects = self.configure_shared()
        when = at('2026-09-09T04:12:00-04:00')
        plan = sigma_plan(when)
        self.save(when.isoformat())
        atomic_write_json(self.sigma.root / 'close-2026-09-08.json', {'spots': {'AAA': 100}})
        self.sigma.publish(plan)
        with tempfile.TemporaryDirectory() as root:
            other = ExpectedSigma(Path(root))
            other.shared = self.sigma.shared
            with patch('scripts.expected_sigma.resolve_historical_api_keys', side_effect=AssertionError('No Databento on S3 hit')):
                other.refresh(bundle(), when)
            row = other.apply(bundle(), when)['today']['rows'][0]
            self.assertEqual(row['sigma_1d_pct'], 2)
            self.assertEqual(row['return_anchor_price'], 100)
            self.assertEqual(row['iv_snapshot_time'], '2026-09-08T20:00:00+00:00')
        self.assertNotIn('z_score', str(objects))

    def test_serving_replica_keeps_worker_values_without_raw_quotes(self):
        import json
        self.configure_shared()
        when = at('2026-09-11T04:12:00-04:00')
        plan = sigma_plan(when)
        self.save(when.isoformat())
        inputs = {'spots': {'AAA': 106.125}, 'source': 'historical',
                  'quotes': [{'symbol': 'AAA   260918C00100000', 'ts_recv': '2026-09-10T20:00:00Z',
                              'bid_px_00': float('nan'), 'ask_px_00': float('inf')}], 'requested_tickers': ['AAA']}
        atomic_write_json(self.sigma.root / 'close-2026-09-10.json', inputs)
        self.sigma.publish(plan)
        with tempfile.TemporaryDirectory() as root:
            replica = ExpectedSigma(Path(root), serving_only=True)
            replica.shared = self.sigma.shared
            # Exercise migration of an existing large input cache as well as S3.
            atomic_write_json(replica.root / 'close-2026-09-10.json', inputs)
            self.assertEqual(replica.apply(bundle(), when)['today']['rows'][0]['return_anchor_price'], 106.125)
            from scripts.expected_sigma import read_json
            with patch('scripts.expected_sigma.read_json', wraps=read_json) as reads:
                replica.apply(bundle(), when)
                self.assertFalse(any(call.args[0].name.startswith('close-') for call in reads.call_args_list))
            replica.sync_shared(plan)
            self.assertEqual(replica.apply(bundle(), when), self.sigma.apply(bundle(), when))
            with patch('scripts.expected_sigma.read_json', side_effect=AssertionError('Unchanged cache must not be decoded again')):
                replica.sync_shared(plan)
            # A repaired/new S3 publication still replaces closing anchors immediately.
            inputs['spots']['AAA'] = 107.25
            atomic_write_json(self.sigma.root / 'close-2026-09-10.json', inputs)
            self.sigma.publish(plan)
            replica.sync_shared(plan)
            self.assertEqual(replica.apply(bundle(), when)['today']['rows'][0]['return_anchor_price'], 107.25)
            retained = json.loads((replica.root / 'close-2026-09-10.json').read_text())
            self.assertEqual(retained['quotes'], [])
            self.assertEqual(retained['spots'], inputs['spots'])
            self.assertEqual(len(json.loads((self.sigma.root / 'close-2026-09-10.json').read_text())['quotes']), 1)
            with self.assertRaisesRegex(RuntimeError, 'cannot reconstruct'):
                replica.refresh(bundle(), when)
            with self.assertRaisesRegex(RuntimeError, 'cannot publish'):
                replica.publish(plan)

    def test_streaming_reader_preserves_strings_and_split_literals(self):
        import io, json
        from scripts.expected_sigma import _FiniteJSONReader
        for padding in range(65500, 65550):
            payload = {'text': 'x' * padding + 'NaN\\"Infinity',
                       'invalid': [float('nan'), float('inf'), -float('inf')], 'spot': 106.125}
            reader = _FiniteJSONReader(io.BytesIO(json.dumps(payload).encode()))
            normalized = b''.join(iter(lambda: reader.read(257), b''))
            decoded = json.loads(normalized)
            self.assertEqual(decoded, {**payload, 'invalid': [None, None, None]})

    def test_truncated_shared_publication_does_not_replace_serving_values(self):
        self.sigma.serving_only = True
        when = at('2026-09-11T04:12:00-04:00')
        plan = sigma_plan(when)
        self.save(when.isoformat())
        before = self.sigma.apply(bundle(), when)
        def download(key, path):
            path.write_text('{"plan": {}, "inputs": {"quotes": [')
            return True
        with patch.object(self.sigma.shared, 'get', side_effect=download):
            with self.assertRaisesRegex(Exception, 'provenance'):
                self.sigma.sync_shared(plan)
        self.assertEqual(self.sigma.apply(bundle(), when), before)

    def test_s3_error_does_not_trigger_paid_fallback(self):
        from scripts.shared_cache import SharedCacheError
        self.configure_shared()
        self.sigma.shared.get = lambda *args: (_ for _ in ()).throw(SharedCacheError('Denied'))
        with patch('scripts.expected_sigma.resolve_historical_api_keys', side_effect=AssertionError('No paid fallback')):
            with self.assertRaises(SharedCacheError):
                self.sigma.refresh(bundle(), at('2026-09-09T04:12:00-04:00'))

    def test_disjoint_publications_preserve_other_tickers(self):
        from scripts.expected_sigma import merge_closing_inputs
        left = {'spots': {'AAA': 100}, 'requested_tickers': ['AAA'],
                'source': 'historical', 'quotes': [{'symbol': 'AAA call', 'ts_recv': 'close'}]}
        right = {'spots': {'BBB': 200}, 'requested_tickers': ['BBB'],
                 'source': 'historical', 'quotes': [{'symbol': 'BBB call', 'ts_recv': 'close'}]}
        merged = merge_closing_inputs(left, right)
        self.assertEqual(merged['requested_tickers'], ['AAA', 'BBB'])
        self.assertEqual(len(merged['quotes']), 2)
        self.assertEqual(merged['spots'], {'AAA': 100, 'BBB': 200})

    def test_closing_input_merge_preserves_fallback_coverage(self):
        from scripts.expected_sigma import merge_closing_inputs
        merged = merge_closing_inputs(
            {'fallback_requested_tickers': ['AAA']},
            {'fallback_requested_tickers': ['BBB']},
        )
        self.assertEqual(merged['fallback_requested_tickers'], ['AAA', 'BBB'])

    def test_wrong_session_publication_rejected(self):
        from scripts.shared_cache import SharedCacheError
        objects = self.configure_shared()
        when = at('2026-09-09T04:12:00-04:00')
        objects['expected-sigma-min2dte-v2/2026-09-09.json'] = {'plan': sigma_plan(at('2026-09-08T04:00:00-04:00')), 'inputs': {}}
        with self.assertRaises(SharedCacheError):
            self.sigma.refresh(bundle(), when)

    def test_partial_current_snapshot_restores_shared_prior_fallback(self):
        objects = self.configure_shared()
        when = at('2026-09-09T04:12:00-04:00')
        previous = sigma_plan(at('2026-09-08T04:12:00-04:00'))
        objects['expected-sigma-min2dte-v2/2026-09-08.json'] = {'plan': previous,
            'rows': {'AAA': {'sigma_1d_pct': 3, 'annualized_implied_vol_pct': 3 / math.sqrt(previous['year_fraction'])}},
            'inputs': {'spots': {'AAA': 100}}}
        with patch('scripts.expected_sigma.resolve_historical_api_keys', return_value=(None, None)):
            with self.assertRaises(RuntimeError):
                self.sigma.refresh(bundle(), when)
        row = self.sigma.apply(bundle(), when)['today']['rows'][0]
        self.assertEqual(row['sigma_1d_pct'], 3)
        self.assertEqual(row['expected_sigma_status'], 'awaiting_previous_close')

    def test_verified_shared_close_is_not_overwritten_by_list_or_live_close(self):
        self.configure_shared()
        when = at('2026-09-09T04:12:00-04:00')
        plan = sigma_plan(when)
        self.save(when.isoformat())
        atomic_write_json(self.sigma.root / 'close-2026-09-08.json', {'spots': {'AAA': 100}})
        self.sigma.publish(plan)
        # A missing second ticker takes the partial refresh path.
        source = bundle()
        source['today']['rows'].append({'ticker': 'BBB'})
        with tempfile.TemporaryDirectory() as root:
            other = ExpectedSigma(Path(root))
            other.shared = self.sigma.shared
            with patch('scripts.expected_sigma.resolve_historical_api_keys', return_value=(None, None)):
                with self.assertRaises(RuntimeError):
                    other.refresh(source, when, regular_prices={'AAA': {'price': 99,
                        'trading_date': '2026-09-08', 'ts_event': '2026-09-08T19:59:55+00:00'}})
            from scripts.expected_sigma import read_json
            self.assertEqual(read_json(other.root / 'close-2026-09-08.json')['spots']['AAA'], 100)

    def test_new_symbols_do_not_redownload_cached_option_quotes(self):
        self.configure_shared()
        when = at('2026-09-09T04:12:00-04:00')
        source = bundle()
        source['today']['rows'].append(dict(ticker='BBB', last_price=200))
        quote = dict(ts_recv='2026-09-08T19:59:59+00:00', symbol='AAA', bid_px_00=1, ask_px_00=2)
        atomic_write_json(self.sigma.root / 'close-2026-09-08.json',
            {'spots': {'AAA': 100}, 'quotes': [quote], 'requested_tickers': ['AAA'], 'source': 'historical'})
        with patch('scripts.expected_sigma.resolve_live_option_api_key', return_value='test'), \
             patch('scripts.expected_sigma.resolve_historical_api_keys', return_value=(None, None)), \
             patch('scripts.expected_sigma.replay_quotes', return_value=[{**quote, 'symbol': 'BBB'}]) as replay, \
             patch('scripts.expected_sigma.prepare_option_pairs', return_value=({}, {})), \
             patch('scripts.expected_sigma.estimate_option_iv', return_value=None):
            self.sigma.refresh(source, when)
        self.assertEqual(replay.call_args.args[1], ['BBB'])
        from scripts.expected_sigma import read_json
        saved = read_json(self.sigma.root / 'close-2026-09-08.json')
        self.assertEqual(saved['requested_tickers'], ['AAA', 'BBB'])
        self.assertEqual(len(saved['quotes']), 2)

    def test_partial_refresh_preserves_published_sigma_for_covered_symbols(self):
        self.configure_shared()
        when = at('2026-09-09T04:12:00-04:00')
        self.save(when.isoformat(), 2)
        source = bundle()
        source['today']['rows'].append(dict(ticker='BBB', last_price=200))
        quote = dict(ts_recv='2026-09-08T19:59:59+00:00', symbol='AAA', bid_px_00=1, ask_px_00=2)
        atomic_write_json(self.sigma.root / 'close-2026-09-08.json',
            {'spots': {'AAA': 100, 'BBB': 200}, 'quotes': [quote], 'requested_tickers': ['AAA', 'BBB'], 'source': 'historical'})
        with patch('scripts.expected_sigma.resolve_historical_api_keys', return_value=(None, None)), \
             patch('scripts.expected_sigma.prepare_option_pairs', return_value=({}, {})), \
             patch('scripts.expected_sigma.estimate_option_iv', return_value=None) as estimate:
            self.sigma.refresh(source, when)
        self.assertEqual({call.args[0]['ticker'] for call in estimate.call_args_list}, {'BBB'})
        self.assertEqual(self.sigma.apply(source, when)['today']['rows'][0]['sigma_1d_pct'], 2)

    def test_failed_anchor_restore_does_not_publish_fresh_sigma(self):
        when = at('2026-09-10T10:00:00-04:00')
        plan = sigma_plan(when)
        publication = {'plan': plan, 'rows': {'AAA': {'sigma_1d_pct': 2,
            'annualized_implied_vol_pct': 2 / math.sqrt(plan['year_fraction'])}},
            'inputs': {'spots': {'AAA': 106}}}
        def download(key, path):
            atomic_write_json(path, publication)
            return True
        def failing_write(path, value):
            if path.name.startswith('close-'):
                raise OSError('Simulated interrupted anchor restore')
            atomic_write_json(path, value)
        with patch.object(self.sigma.shared, 'get', side_effect=download), patch(
                'scripts.expected_sigma.atomic_write_json', side_effect=failing_write):
            with self.assertRaises(OSError): self.sigma.sync_shared(plan)
        self.assertEqual(self.sigma.apply(bundle(), when)['expected_sigma']['status'], 'awaiting_previous_close')
        with patch.object(self.sigma.shared, 'get', side_effect=download):
            self.sigma.sync_shared(plan)
        result = self.sigma.apply(bundle(), when)
        self.assertEqual(result['expected_sigma']['status'], 'fresh')
        self.assertEqual(result['today']['rows'][0]['return_anchor_price'], 106)

    def test_replay_retains_only_requested_expiry_before_caching(self):
        from unittest.mock import MagicMock
        from types import SimpleNamespace
        from scripts.expected_sigma import replay_quotes
        class Mapping(SimpleNamespace): pass
        class System(SimpleNamespace): pass
        client = MagicMock()
        close = at('2026-09-16T20:00:00+00:00')
        def emit():
            receive = client.add_callback.call_args.args[0]
            for instrument, symbol in [(1, 'AAA   260918C00100000'), (2, 'AAA   260925C00100000')]:
                receive(Mapping(instrument_id=instrument, stype_out_symbol=symbol))
                receive(SimpleNamespace(instrument_id=instrument, ts_recv=int(close.timestamp()*1e9),
                                        pretty_bid_px_00=1, pretty_ask_px_00=2))
            receive(System(code=3))
        client.start.side_effect = emit
        with patch('scripts.expected_sigma.db.Live', return_value=client), \
             patch('scripts.expected_sigma.db.SymbolMappingMsg', Mapping), \
             patch('scripts.expected_sigma.db.SystemMsg', System):
            quotes = replay_quotes('test', ['AAA'], close, expiry='2026-09-25')
        self.assertEqual([q['symbol'] for q in quotes], ['AAA   260925C00100000'])
