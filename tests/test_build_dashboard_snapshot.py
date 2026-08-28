import unittest

from scripts.build_dashboard_snapshot import build_payload


class DashboardSnapshotTests(unittest.TestCase):
    def test_builds_top_gainer_payload_without_external_company_snapshot(self):
        screen = {
            "requested_window": {
                "start_date": "2026-08-26",
                "end_date": "2026-08-27",
            },
            "market_data": {
                "spot": {"dataset": "EQUS.MINI"},
                "options": {
                    "snapshot_observation_time": "2026-08-26T16:00:00-04:00",
                    "expiry": "2026-08-28",
                },
            },
            "coverage": {"symbols_with_valid_option_iv": 42},
            "results": [{
                "rank": 1,
                "ticker": "AAA",
                "company_name": "Alpha Inc.",
                "sector": "Technology",
                "industry": "Software",
                "market_cap_usd": 12_000_000_000,
                "anchor_price": 100.0,
                "end_close": 108.0,
                "weekly_return_pct": 8.0,
                "realized_horizon_implied_sigma_pct": 2.5,
                "realized_horizon_implied_z": 3.08,
                "option_snapshot": {"iv_reliability": "high"},
            }],
        }

        payload = build_payload(screen)

        self.assertEqual(payload["rows"][0]["ticker"], "AAA")
        self.assertEqual(payload["valid_iv_coverage"], 42)
        self.assertIn("Software", payload["rows"][0]["company_description"])


if __name__ == "__main__":
    unittest.main()
