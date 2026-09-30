import unittest
from datetime import datetime

from scripts.history_repair_worker import calibration_plan


class CalibrationPlanTests(unittest.TestCase):
    def test_after_close_prepares_next_session_before_premarket(self):
        activation, plan = calibration_plan(datetime.fromisoformat("2026-09-29T20:40:00-04:00"))
        self.assertEqual(activation.isoformat(), "2026-09-30T04:00:00-04:00")
        self.assertEqual(plan["effective_session"], "2026-09-30")
        self.assertEqual(plan["iv_session"], "2026-09-29")

    def test_premarket_keeps_same_completed_session(self):
        _, plan = calibration_plan(datetime.fromisoformat("2026-09-30T03:45:00-04:00"))
        self.assertEqual(plan["iv_session"], "2026-09-29")

    def test_exchange_holiday_uses_last_completed_session(self):
        _, plan = calibration_plan(datetime.fromisoformat("2026-11-26T20:40:00-05:00"))
        self.assertEqual(plan["effective_session"], "2026-11-27")
        self.assertEqual(plan["iv_session"], "2026-11-25")

    def test_reexec_can_finish_original_session_after_next_close(self):
        _, plan = calibration_plan(
            datetime.fromisoformat("2026-09-30T17:00:00-04:00"),
            effective_session="2026-09-30",
        )
        self.assertEqual(plan["iv_session"], "2026-09-29")

    def test_rejects_future_closing_input(self):
        with self.assertRaisesRegex(RuntimeError, "before the reference session closes"):
            calibration_plan(
                datetime.fromisoformat("2026-09-29T15:00:00-04:00"),
                effective_session="2026-09-30",
            )


if __name__ == "__main__":
    unittest.main()
