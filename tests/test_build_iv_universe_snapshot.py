import unittest

from scripts.build_iv_universe_snapshot import build_payload


class IvUniverseSnapshotTests(unittest.TestCase):
    def test_keeps_only_valid_iv_candidates_and_anchors_to_end_close(self):
        screen = {
            "analysis_as_of": "2026-08-28T00:00:00Z",
            "requested_window": {"start_date": "2026-08-26", "end_date": "2026-08-27"},
            "market_data": {
                "spot": {"dataset": "EQUS.MINI"},
                "options": {
                    "snapshot_observation_time": "2026-08-26T16:00:00-04:00",
                    "expiry": "2026-08-28",
                },
            },
            "candidate_manifest": [
                {
                    "ticker": "AAA",
                    "company_name": "Alpha Inc.",
                    "sector": "Technology",
                    "industry": "Software",
                    "market_cap_usd": 5_000_000_000,
                    "option_status": "valid_iv",
                    "end_close": 105.0,
                    "weekly_return_pct": 5.0,
                    "realized_horizon_implied_z": 1.2,
                    "realized_horizon_implied_sigma_pct": 2.5,
                    "annualized_implied_vol_pct": 47.0,
                    "iv_reliability": "high",
                },
                {"ticker": "BBB", "option_status": "no_valid_iv"},
            ],
        }

        payload = build_payload(screen)

        self.assertEqual(payload["valid_iv_coverage"], 1)
        self.assertEqual(payload["rows"][0]["ticker"], "AAA")
        self.assertEqual(payload["rows"][0]["anchor_price"], 105.0)
        self.assertEqual(payload["rows"][0]["z_score"], 0.0)
        self.assertEqual(payload["live_calculation"]["ranking"], "absolute live z-score")


if __name__ == "__main__":
    unittest.main()
