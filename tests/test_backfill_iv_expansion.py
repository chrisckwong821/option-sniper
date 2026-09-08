import unittest
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from scripts.backfill_iv_expansion import (
    option_events,
    replay_bounds,
    resolve_replay_selection,
    schema_available_end,
    select_rows,
    spot_events,
    targeted_catalog_cache_path,
)


class BackfillIvExpansionTests(unittest.TestCase):
    def test_select_rows_keeps_positive_z_references(self):
        rows = [
            {"ticker": "AAA", "z_score": 1.2, "iv_expansion_reference": {"method": "x"}},
            {"ticker": "BBB", "z_score": -1.2, "iv_expansion_reference": {"method": "x"}},
            {"ticker": "CCC", "z_score": 2.0},
        ]
        self.assertEqual([row["ticker"] for row in select_rows(rows, None)], ["AAA"])
        self.assertEqual(select_rows(rows, ["bbb"]), [])

    def test_select_rows_skips_already_completed_tickers(self):
        rows = [
            {"ticker": "AAA", "z_score": 1.2, "iv_expansion_reference": {"method": "x"}},
            {"ticker": "BBB", "z_score": 1.4, "iv_expansion_reference": {"method": "x"}},
        ]

        self.assertEqual(
            [row["ticker"] for row in select_rows(rows, None, {"AAA"})],
            ["BBB"],
        )

    def test_targeted_catalog_cache_is_stable_and_does_not_replace_base(self):
        base = Path(".cache/sigma-tape/option-contracts.json")
        first = targeted_catalog_cache_path(
            base,
            [{"ticker": "BBB"}, {"ticker": "AAA"}],
            as_of_date="2026-09-04",
        )
        reordered = targeted_catalog_cache_path(
            base,
            [{"ticker": "AAA"}, {"ticker": "BBB"}],
            as_of_date="2026-09-04",
        )

        self.assertEqual(first, reordered)
        self.assertNotEqual(first, base)
        self.assertIn("option-contracts.2026-09-04.", first.name)

    def test_replay_bounds_ends_just_after_regular_close(self):
        self.assertEqual(
            replay_bounds("2026-09-04", 30),
            ("2026-09-04T15:30:00-04:00", "2026-09-04T16:00:01-04:00"),
        )

    def test_events_use_equity_before_options_at_same_timestamp(self):
        timestamp = pd.Timestamp("2026-09-04T19:59:00Z")
        spots = spot_events(pd.DataFrame({
            "ts_event": [timestamp], "symbol": ["AAA"], "close": [101.5]
        }))
        options = option_events(pd.DataFrame({
            "ts_recv": [timestamp],
            "symbol": ["AAA   260911C00100000"],
            "bid_px_00": [1.0],
            "ask_px_00": [1.2],
        }))
        events = sorted([*options, *spots], key=lambda event: (event[0], event[1]))
        self.assertEqual(events[0][1], 0)
        self.assertEqual(events[1][1], 1)

    def test_schema_available_end_prefers_the_requested_schema(self):
        value = schema_available_end({
            "end": "2026-09-06T00:00:00Z",
            "schema": {"cbbo-1m": {"end": "2026-09-04T00:00:00Z"}},
        }, "cbbo-1m")
        self.assertEqual(value.isoformat(), "2026-09-04T00:00:00+00:00")

    def test_replay_prefers_yesterday_when_the_full_window_is_available(self):
        selection = resolve_replay_selection(
            "2026-09-04",
            datetime(2026, 9, 5, tzinfo=UTC),
            datetime(2026, 9, 5, tzinfo=UTC),
            30,
            5,
        )

        self.assertIsNotNone(selection)
        self.assertEqual(selection.observation_session, "2026-09-04")
        self.assertEqual(selection.reference_session, "2026-09-03")
        self.assertEqual(selection.reference_status, "yesterday")
        self.assertEqual(selection.fallback_sessions, 0)

    def test_replay_falls_back_when_yesterday_is_not_fully_available(self):
        selection = resolve_replay_selection(
            "2026-09-04",
            datetime(2026, 9, 4, tzinfo=UTC),
            datetime(2026, 9, 5, tzinfo=UTC),
            30,
            5,
        )

        self.assertIsNotNone(selection)
        self.assertEqual(selection.observation_session, "2026-09-03")
        self.assertEqual(selection.reference_session, "2026-09-02")
        self.assertEqual(selection.reference_status, "fallback")
        self.assertEqual(selection.fallback_sessions, 1)

    def test_replay_rejects_a_partially_available_yesterday_window(self):
        selection = resolve_replay_selection(
            "2026-09-04",
            datetime(2026, 9, 4, 19, 50, tzinfo=UTC),
            datetime(2026, 9, 5, tzinfo=UTC),
            30,
            5,
        )

        self.assertIsNotNone(selection)
        self.assertEqual(selection.observation_session, "2026-09-03")


if __name__ == "__main__":
    unittest.main()
