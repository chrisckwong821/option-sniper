import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.dashboard_state import build_mock_bundle, screening_policy_fingerprint
from scripts.shared_cache import SharedCacheError
from scripts.universe_input import publish, restore
from tests.test_dashboard_state import sample_plan


class UniverseInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = build_mock_bundle(sample_plan(), 30)
        self.session = self.state["today"]["anchor_date"]
        self.objects = {}
        self.shared = Mock(enabled=True)

        def put(key, path):
            self.objects[key] = Path(path).read_bytes()

        def get(key, path, checksum=None):
            if key not in self.objects:
                return False
            Path(path).write_bytes(self.objects[key])
            return True

        self.shared.put.side_effect = put
        self.shared.get.side_effect = get

    def test_current_universe_roundtrips_without_computed_bundle(self):
        with patch("scripts.universe_input.SharedCache", return_value=self.shared):
            key = publish(self.root, self.state)
            restored = restore(
                self.root / "worker",
                self.session,
                screening_policy_fingerprint(),
            )
        self.assertEqual(key, f"universe/{self.session}.json")
        self.assertEqual(restored, self.state)
        self.assertEqual(
            json.loads((self.root / "worker" / "dashboard-state.json").read_text()),
            self.state,
        )

    def test_wrong_session_is_rejected_without_replacing_local_state(self):
        target = self.root / "worker"
        target.mkdir()
        existing = target / "dashboard-state.json"
        existing.write_text("old")
        with patch("scripts.universe_input.SharedCache", return_value=self.shared):
            publish(self.root, self.state)
            with self.assertRaisesRegex(SharedCacheError, "missing"):
                restore(target, "2026-08-26", screening_policy_fingerprint())
        self.assertEqual(existing.read_text(), "old")

    def test_tampered_provenance_is_rejected(self):
        with patch("scripts.universe_input.SharedCache", return_value=self.shared):
            key = publish(self.root, self.state)
            payload = json.loads(self.objects[key])
            payload["completed_session"] = "2026-08-26"
            self.objects[key] = json.dumps(payload).encode()
            with self.assertRaisesRegex(SharedCacheError, "provenance"):
                restore(self.root / "worker", self.session, screening_policy_fingerprint())

    def test_overnight_yesterday_refresh_publishes_completed_universe(self):
        from scripts.refresh_dashboard_cache import main

        self.state["cache"] = {"status": "fresh", "source": "generated"}
        state_cache = self.root / "yesterday-state.json"
        with (
            patch("scripts.refresh_dashboard_cache.build_refresh_plan", return_value=sample_plan()),
            patch("scripts.refresh_dashboard_cache.DashboardStateManager") as manager,
            patch("scripts.universe_input.SharedCache", return_value=self.shared),
            patch.object(sys, "argv", ["refresh_dashboard_cache", "--state-cache", str(state_cache)]),
            redirect_stdout(StringIO()),
        ):
            manager.return_value.ensure_state.return_value = self.state
            main()

        payload = json.loads(self.objects[f"universe/{self.session}.json"])
        self.assertEqual(payload["completed_session"], self.session)
        self.assertEqual(payload["state"], self.state)
