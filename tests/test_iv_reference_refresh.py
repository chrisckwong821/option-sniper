import copy
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from scripts.iv_reference_refresh import activate_references, align_serving_expiry


class ReferenceRefreshTests(unittest.TestCase):
    def test_repair_invalidates_old_score_and_preserves_live_quotes(self):
        old = {'reference_session': '2026-09-04', 'reference_status': 'fallback',
               'occ_root': 'AAA', 'baseline_revision': 1}
        quotes = {'AAA': {'live': 'quotes'}}
        tracker = SimpleNamespace(option_expiry_close=datetime.fromisoformat('2026-09-11T20:00:00+00:00'),
            references={'AAA': old}, results={'AAA': {'iv_expansion_pct': 90}}, next_calculation={'AAA': 999},
            spots={'AAA': {'price': 100}}, quotes=quotes, _schedule_calculation=Mock(), cache_dirty=False)
        store = SimpleNamespace(iv_tracker=tracker, dirty_symbols=set(), invalidate_snapshot=Mock())
        plan = {'iv_session': '2026-09-08', 'expiry_close': '2026-09-11T20:00:00+00:00'}
        ref = {'reference_session': '2026-09-08', 'reference_status': 'yesterday', 'reference_iv_pct': 30}
        self.assertEqual(activate_references(store, plan, {'AAA': ref}), 1)
        self.assertEqual(tracker.references['AAA']['occ_root'], 'AAA')
        self.assertEqual(tracker.references['AAA']['baseline_revision'], 2)
        self.assertNotIn('AAA', tracker.results)
        self.assertIs(tracker.quotes, quotes)
        tracker._schedule_calculation.assert_called_once()
        self.assertEqual(activate_references(store, plan, {'AAA': ref}), 0)

    def test_different_expiry_keeps_active_reference(self):
        tracker = SimpleNamespace(option_expiry_close=datetime.fromisoformat('2026-09-11T20:00:00+00:00'))
        store = SimpleNamespace(iv_tracker=tracker)
        self.assertEqual(activate_references(store, {'expiry_close': '2026-09-18T20:00:00+00:00'}, {'AAA': {}}), 0)

    def test_rollover_clears_wrong_expiry_and_hydrates_matching_reference(self):
        row = {'ticker': 'AAA', 'iv_chain': {'occ_root': 'AAA', 'strikes': [100]},
               'iv_expansion_reference': {'option_expiry': '2026-09-11'}}
        bundle = {'refresh_plan': {}, 'today': {'rows': [row]}, 'snapshot': {'rows': [row]}}
        plan = {'option_expiry': '2026-09-18', 'expiry_close': '2026-09-18T20:00:00+00:00'}
        waiting = align_serving_expiry(bundle, plan, {})
        self.assertEqual(waiting['today']['rows'][0]['iv_expansion_reference'], {})
        self.assertEqual(waiting['today']['rows'][0]['iv_chain'], {'occ_root': 'AAA', 'strikes': []})
        ref = {'reference_iv_pct': 30, 'reference_forward': 101,
               'reference_strikes': [99, 101], 'moneyness_targets': [-.02, 0],
               'option_expiry': '2026-09-18'}
        ready = align_serving_expiry(bundle, plan, {'AAA': ref})
        for view in ('today', 'snapshot'):
            self.assertEqual(ready[view]['rows'][0]['iv_expansion_reference']['option_expiry'], '2026-09-18')
            self.assertEqual(ready[view]['rows'][0]['iv_chain']['strikes'], [99, 101])
        self.assertEqual(row['iv_expansion_reference']['option_expiry'], '2026-09-11')

    def test_delayed_reference_activates_pending_root(self):
        tracker = SimpleNamespace(option_expiry_close=datetime.fromisoformat('2026-09-18T20:00:00+00:00'),
            references={}, pending_roots={'AAA': 'AAA'}, ticker_by_root={}, results={},
            next_calculation={}, spots={}, cache_dirty=False)
        store = SimpleNamespace(iv_tracker=tracker, dirty_symbols=set(), invalidate_snapshot=Mock())
        plan = {'iv_session': '2026-09-09', 'expiry_close': '2026-09-18T20:00:00+00:00'}
        ref = {'reference_session': '2026-09-09', 'reference_status': 'yesterday', 'reference_iv_pct': 30}
        self.assertEqual(activate_references(store, plan, {'AAA': ref}), 1)
        self.assertEqual(tracker.ticker_by_root['AAA'], 'AAA')
        self.assertEqual(tracker.references['AAA']['reference_iv_pct'], 30)
