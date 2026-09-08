import unittest
from datetime import datetime
from scripts.dashboard_state import build_refresh_plan
from scripts.refresh_dashboard_cache import is_reopening_refresh_due

class ReopeningRefreshTests(unittest.TestCase):
    def test_calendar_and_time_boundaries(self):
        cases = [
            ('2026-08-30T19:44:59-04:00', False),
            ('2026-08-30T19:45:00-04:00', True),
            ('2026-08-30T20:45:00-04:00', True),
            ('2026-08-31T20:00:00-04:00', False),
            ('2026-09-06T20:00:00-04:00', False),
            ('2026-09-07T20:00:00-04:00', True),
            ('2026-11-26T20:00:00-05:00', True),
            ('2026-11-29T20:00:00-05:00', True),
            ('2026-09-08T04:00:00-04:00', False),
        ]
        for timestamp, expected in cases:
            with self.subTest(timestamp=timestamp):
                now = datetime.fromisoformat(timestamp)
                self.assertEqual(is_reopening_refresh_due(build_refresh_plan(now), now), expected)
