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
            partial = scores.score({**point, 'last_valid_at': '2026-09-08T19:45:00+00:00'}, ref)
            self.assertAlmostEqual(partial['z'], scores.score(point, ref)['z'])
            self.assertTrue(partial['indicative'])
            self.assertFalse(scores.score(point, ref)['indicative'])
            self.assertIsNone(scores.score(point, {'option_expiry': '2026-09-18'})['z'])
            self.assertIsNone(scores.score({**point, 'iv_expansion_pct': -100}, ref)['z'])
            self.assertTrue(scores.score({**point, 'iv_reliability': 'low'}, ref)['provisional'])
            self.assertIsNone(scores.score({**point, 'last_valid_at': '2026-09-03T19:59:00+00:00'}, {'option_expiry': '2026-09-06'})['z'])

    def test_missing_history_is_not_zero(self):
        self.assertEqual(BucketScores(Path('/nonexistent/stats.json')).rows, {})

    def test_quote_time_buckets_and_session_guards(self):
        scores = BucketScores(Path('/nonexistent/stats.json'))
        for stamp, bucket in [
            ('2026-09-08T14:12:00+00:00', '10:00'),
            ('2026-09-08T14:37:00+00:00', '10:30'),
            ('2026-01-06T15:12:00+00:00', '10:00'),
        ]:
            with self.subTest(stamp=stamp):
                result = scores.score({'last_valid_at': stamp}, {})
                self.assertEqual(result['bucket'], bucket)
                self.assertTrue(result['indicative'])
        for stamp in [
            '2026-09-08T10:00:00',  # timezone missing
            '2026-09-08T13:29:00+00:00',  # premarket
            '2026-09-08T20:00:00+00:00',  # closed
            '2026-09-07T15:00:00+00:00',  # holiday
            '2026-11-27T18:00:00+00:00',  # early close
        ]:
            with self.subTest(stamp=stamp):
                self.assertIsNone(scores.score({'last_valid_at': stamp}, {})['bucket'])
