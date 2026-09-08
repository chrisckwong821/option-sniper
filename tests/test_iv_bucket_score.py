import json
import math
import tempfile
import unittest
from pathlib import Path
from scripts.iv_bucket_score import BucketScores


class BucketScoreTests(unittest.TestCase):
    def test_score_and_comparability_guards(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'stats.json'
            path.write_text(json.dumps({'last_calibration_session': '2026-09-04', 'statistics': [{
                'ticker': 'AAA', 'bucket': '15:30', 'tenor_class': 'front_weekly',
                'method': 'moneyness-matched-black76-robust-mad-min2dte-v2',
                'calibration_excludes_session': '2026-09-04',
                'log_iv_change_distribution': {'status': 'valid', 'n': 60, 'median': 2, 'robust_sigma': 4}}]}))
            scores = BucketScores(path)
            point = {'ticker': 'AAA', 'last_valid_at': '2026-09-08T19:59:00+00:00',
                     'iv_expansion_pct': 12, 'iv_reference_status': 'yesterday', 'iv_reliability': 'high'}
            ref = {'option_expiry': '2026-09-11'}
            self.assertAlmostEqual(scores.score(point, ref)['z'], (100 * math.log(1.12) - 2) / 4)
            self.assertIsNone(scores.score({**point, 'iv_reference_status': 'fallback'}, ref)['z'])
            self.assertIsNone(scores.score({**point, 'last_valid_at': '2026-09-08T19:45:00+00:00'}, ref)['z'])
            self.assertIsNone(scores.score(point, {'option_expiry': '2026-09-18'})['z'])
            self.assertIsNone(scores.score({**point, 'iv_expansion_pct': -100}, ref)['z'])
            self.assertTrue(scores.score({**point, 'iv_reliability': 'low'}, ref)['provisional'])
            self.assertIsNone(scores.score({**point, 'last_valid_at': '2026-09-03T19:59:00+00:00'}, {'option_expiry': '2026-09-06'})['z'])

    def test_missing_history_is_not_zero(self):
        self.assertEqual(BucketScores(Path('/nonexistent/stats.json')).rows, {})
