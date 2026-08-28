import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from scripts.dashboard_state import (
    DashboardStateManager,
    RefreshPlan,
    atomic_write_json,
    build_mock_bundle,
    build_refresh_plan,
    validate_bundle,
)


def sample_plan(completed_session: str = "2026-08-27") -> RefreshPlan:
    return RefreshPlan(
        completed_session=completed_session,
        prior_session="2026-08-26",
        next_session="2026-08-28",
        option_expiry="2026-08-28",
        option_expiry_close="2026-08-28T16:00:00-04:00",
        completed_session_close_utc="2026-08-27T20:00:00+00:00",
        option_snapshot_start="2026-08-26T15:59:00-04:00",
        option_snapshot_end="2026-08-26T16:00:01-04:00",
        option_valuation_time="2026-08-26T16:00:00-04:00",
    )


class RefreshPlanTests(unittest.TestCase):
    def test_friday_premarket_uses_thursday_as_completed_session(self):
        plan = build_refresh_plan(datetime(2026, 8, 28, 12, 0, tzinfo=UTC))
        self.assertEqual(plan.completed_session, "2026-08-27")
        self.assertEqual(plan.prior_session, "2026-08-26")
        self.assertEqual(plan.next_session, "2026-08-28")
        self.assertEqual(plan.option_expiry, "2026-08-28")

    def test_friday_after_close_uses_friday(self):
        plan = build_refresh_plan(datetime(2026, 8, 28, 21, 0, tzinfo=UTC))
        self.assertEqual(plan.completed_session, "2026-08-28")
        self.assertEqual(plan.next_session, "2026-08-31")
        self.assertEqual(plan.option_expiry, "2026-09-04")

    def test_exchange_holiday_moves_weekly_expiry_to_prior_session(self):
        plan = build_refresh_plan(datetime(2026, 4, 1, 21, 0, tzinfo=UTC))
        self.assertEqual(plan.completed_session, "2026-04-01")
        self.assertEqual(plan.next_session, "2026-04-02")
        self.assertEqual(plan.option_expiry, "2026-04-02")

    def test_early_close_is_retained_for_horizon_scaling(self):
        plan = build_refresh_plan(datetime(2026, 11, 27, 19, 0, tzinfo=UTC))
        self.assertEqual(plan.completed_session, "2026-11-27")
        self.assertEqual(plan.completed_session_close_utc, "2026-11-27T18:00:00+00:00")


class DashboardStateManagerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.cache = Path(self.tempdir.name) / "state.json"
        self.plan = sample_plan()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_fresh_cache_is_reused_without_refresh(self):
        atomic_write_json(self.cache, build_mock_bundle(self.plan))
        manager = DashboardStateManager(
            self.cache,
            refresh=lambda _: self.fail("fresh cache should not refresh"),
        )
        state = manager.ensure_state(self.plan)
        self.assertEqual(state["cache"]["status"], "fresh")
        self.assertEqual(state["cache"]["source"], "cache")

    def test_stale_cache_is_fallback_when_refresh_fails(self):
        old_plan = sample_plan("2026-08-26")
        atomic_write_json(self.cache, build_mock_bundle(old_plan))
        manager = DashboardStateManager(
            self.cache,
            refresh=lambda _: (_ for _ in ()).throw(RuntimeError("provider offline")),
        )
        state = manager.ensure_state(self.plan)
        self.assertEqual(state["cache"]["status"], "stale")
        self.assertEqual(state["cache"]["source"], "cache-fallback")
        self.assertIn("provider offline", state["cache"]["last_refresh_error"])

    def test_missing_cache_and_failed_refresh_stops_startup(self):
        manager = DashboardStateManager(
            self.cache,
            refresh=lambda _: (_ for _ in ()).throw(RuntimeError("no credentials")),
        )
        with self.assertRaisesRegex(RuntimeError, "Dashboard state refresh failed"):
            manager.ensure_state(self.plan)

    def test_atomic_cache_contains_no_runtime_error_metadata(self):
        bundle = build_mock_bundle(self.plan)
        atomic_write_json(self.cache, bundle)
        persisted = json.loads(self.cache.read_text())
        self.assertNotIn("cache", persisted)
        self.assertEqual(persisted["snapshot"]["data_date"], "2026-08-27")

    def test_successful_refresh_is_persisted_and_marked_fresh(self):
        refreshed = build_mock_bundle(self.plan)
        calls = []
        manager = DashboardStateManager(
            self.cache,
            refresh=lambda plan: calls.append(plan.completed_session) or refreshed,
        )

        state = manager.ensure_state(self.plan)

        self.assertEqual(calls, ["2026-08-27"])
        self.assertEqual(state["cache"]["status"], "fresh")
        self.assertEqual(state["cache"]["source"], "refresh")
        self.assertTrue(self.cache.exists())
        self.assertNotIn("cache", json.loads(self.cache.read_text()))

    def test_refresh_disabled_requires_a_cache(self):
        manager = DashboardStateManager(self.cache)
        with self.assertRaisesRegex(RuntimeError, "cache is missing"):
            manager.ensure_state(self.plan, mode="never")

    def test_corrupt_cache_is_ignored_and_replaced(self):
        self.cache.write_text("not json")
        manager = DashboardStateManager(
            self.cache,
            refresh=lambda _: build_mock_bundle(self.plan),
        )
        state = manager.ensure_state(self.plan)
        self.assertEqual(state["cache"]["source"], "refresh")
        self.assertEqual(json.loads(self.cache.read_text())["schema_version"], "1.0")

    def test_invalid_or_wrong_date_refresh_is_rejected(self):
        invalid = build_mock_bundle(sample_plan("2026-08-26"))
        manager = DashboardStateManager(self.cache, refresh=lambda _: invalid)
        with self.assertRaisesRegex(RuntimeError, "target completed session"):
            manager.ensure_state(self.plan)

        empty = build_mock_bundle(self.plan)
        empty["today"]["rows"] = []
        with self.assertRaisesRegex(ValueError, "today payload has no rows"):
            validate_bundle(empty)

    def test_mock_mode_does_not_write_or_call_refresh(self):
        manager = DashboardStateManager(
            self.cache,
            refresh=lambda _: self.fail("mock mode should not refresh"),
        )
        state = manager.ensure_state(self.plan, mock=True)
        self.assertEqual(state["cache"]["status"], "mock")
        self.assertFalse(self.cache.exists())


if __name__ == "__main__":
    unittest.main()
