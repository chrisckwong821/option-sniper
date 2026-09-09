import copy
import unittest
from scripts.verify_sigma_parity import differences


def state():
    row = dict(ticker='AAA', sigma_1d_pct=2, annualized_implied_vol_pct=38,
               return_anchor_price=100, expected_sigma_status='fresh',
               iv_snapshot_time='2026-09-08T20:00:00+00:00', option_expiry='2026-09-11',
               return_anchor_date='2026-09-08', z_score=1)
    return {'expected_sigma': dict(version='v1', effective_session='2026-09-09', iv_session='2026-09-08',
            iv_snapshot_time=row['iv_snapshot_time'], horizon_end='2026-09-09T20:00:00+00:00', option_expiry='2026-09-11'),
            **{v: {'rows': [copy.deepcopy(row)]} for v in ('today','snapshot')}}


class ParityTests(unittest.TestCase):
    def test_live_z_can_change_while_sigma_matches(self):
        a, b = state(), state()
        b['today']['rows'][0]['z_score'] = 3
        self.assertEqual(differences(a, b), [])

    def test_equivalent_timestamp_offsets_match(self):
        a, b = state(), state()
        b['today']['rows'][0]['iv_snapshot_time'] = '2026-09-08T16:00:00-04:00'
        self.assertEqual(differences(a, b), [])

    def test_rejects_sigma_anchor_date_coverage_and_nonfinite_values(self):
        for field, value in [('sigma_1d_pct', 3), ('return_anchor_price', 101),
                             ('sigma_1d_pct', float('nan')), ('iv_snapshot_time', 'Friday'),
                             ('expected_sigma_status', 'awaiting_previous_close')]:
            with self.subTest(field=field, value=value):
                a, b = state(), state()
                b['today']['rows'][0][field] = value
                self.assertTrue(differences(a, b))
        a, b = state(), state()
        b['snapshot']['rows'] = []
        self.assertTrue(differences(a, b))
        b = state(); b['expected_sigma']['iv_session'] = '2026-09-04'
        self.assertTrue(differences(a, b))
