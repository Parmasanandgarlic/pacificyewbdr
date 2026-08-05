import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import schedule_control


PACIFIC = ZoneInfo("America/Vancouver")


class ScheduleControlTests(unittest.TestCase):
    def test_delayed_github_start_within_slot_hour_is_due(self):
        due, reason = schedule_control.slot_is_due(
            "morning",
            datetime(2026, 8, 5, 8, 23, tzinfo=PACIFIC),
        )
        self.assertTrue(due, reason)

    def test_late_recovery_candidate_is_due_before_window_expires(self):
        due, reason = schedule_control.slot_is_due(
            "morning",
            datetime(2026, 8, 5, 9, 37, tzinfo=PACIFIC),
        )
        self.assertTrue(due, reason)

    def test_slot_is_not_due_before_its_local_time(self):
        due, reason = schedule_control.slot_is_due(
            "late_morning",
            datetime(2026, 8, 5, 9, 37, tzinfo=PACIFIC),
        )
        self.assertFalse(due)
        self.assertIn("not due", reason)

    def test_slot_recovery_window_expires_before_next_delivery_slot(self):
        due, reason = schedule_control.slot_is_due(
            "morning",
            datetime(2026, 8, 5, 9, 46, tzinfo=PACIFIC),
        )
        self.assertFalse(due)
        self.assertIn("expired", reason)

    def test_weekend_slots_are_never_due(self):
        due, reason = schedule_control.slot_is_due(
            "morning",
            datetime(2026, 8, 8, 8, 7, tzinfo=PACIFIC),
        )
        self.assertFalse(due)
        self.assertIn("weekdays", reason)

    def test_partial_failed_attempt_reduces_recovery_capacity(self):
        attempts = [
            {
                "status": "FAILED",
                "sent_ledger_count_at_claim": "100",
                "sent_count": "3",
            }
        ]
        self.assertEqual(schedule_control.prior_slot_sent_count(attempts, 103), 3)

    def test_stale_started_attempt_reconciles_from_ledger_delta(self):
        attempts = [
            {
                "status": "STARTED",
                "sent_ledger_count_at_claim": "200",
                "sent_count": "",
            }
        ]
        self.assertEqual(schedule_control.prior_slot_sent_count(attempts, 205), 5)

    def test_multiple_attempts_do_not_double_count_reconciled_sends(self):
        attempts = [
            {
                "status": "FAILED",
                "sent_ledger_count_at_claim": "300",
                "sent_count": "3",
            },
            {
                "status": "STARTED",
                "sent_ledger_count_at_claim": "303",
                "sent_count": "",
            },
        ]
        self.assertEqual(schedule_control.prior_slot_sent_count(attempts, 305), 5)


if __name__ == "__main__":
    unittest.main()
