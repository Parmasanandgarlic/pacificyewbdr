import os
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import auto_schedule

PACIFIC = ZoneInfo("America/Vancouver")


class AutoClaimScheduleTests(unittest.TestCase):
    def test_auto_claim_prefers_oldest_unfinished_due_slot(self):
        now = datetime(2026, 8, 5, 9, 10, tzinfo=PACIFIC)
        with patch.object(auto_schedule.schedule_control, "claim_slot") as claim:
            claim.return_value = {"should_run": "true", "run_slot": "morning", "send_limit": "8", "attempt_id": "attempt", "reason": "slot_claimed"}
            result = auto_schedule.claim_due_slot(8, now=now)
        self.assertEqual(result["run_slot"], "morning")
        claim.assert_called_once_with("morning", 8, now)

    def test_auto_claim_skips_completed_due_slot(self):
        now = datetime(2026, 8, 5, 12, 20, tzinfo=PACIFIC)
        responses = {
            "morning": {"should_run": "false", "run_slot": "morning", "send_limit": "0", "attempt_id": "", "reason": "slot_cap_already_reached"},
            "late_morning": {"should_run": "false", "run_slot": "late_morning", "send_limit": "0", "attempt_id": "", "reason": "slot_already_completed"},
            "midday": {"should_run": "true", "run_slot": "midday", "send_limit": "8", "attempt_id": "attempt", "reason": "slot_claimed"},
        }
        with patch.object(auto_schedule.schedule_control, "claim_slot", side_effect=lambda slot, run_cap, current: responses[slot]):
            result = auto_schedule.claim_due_slot(8, now=now)
        self.assertEqual(result["run_slot"], "midday")

    def test_auto_claim_returns_noop_when_nothing_is_due(self):
        now = datetime(2026, 8, 5, 7, 30, tzinfo=PACIFIC)
        result = auto_schedule.claim_due_slot(8, now=now)
        self.assertEqual(result["should_run"], "false")
        self.assertEqual(result["reason"], "no_due_slot")

    def test_same_day_catchup_recovers_oldest_missed_slot(self):
        now = datetime(2026, 8, 6, 13, 45, tzinfo=PACIFIC)
        response = {
            "should_run": "true",
            "run_slot": "morning",
            "send_limit": "8",
            "attempt_id": "catchup",
            "reason": "slot_claimed",
        }
        with patch.dict(
            os.environ,
            {
                "ALLOW_SAME_DAY_CATCHUP": "true",
                "SAME_DAY_CATCHUP_CUTOFF_MINUTES": "1020",
            },
            clear=False,
        ), patch.object(auto_schedule.schedule_control, "claim_slot", return_value=response) as claim:
            result = auto_schedule.claim_due_slot(8, now=now)
        self.assertEqual(result["run_slot"], "morning")
        claim.assert_called_once_with("morning", 8, now)

    def test_same_day_catchup_does_not_run_after_cutoff(self):
        now = datetime(2026, 8, 6, 17, 1, tzinfo=PACIFIC)
        with patch.dict(
            os.environ,
            {
                "ALLOW_SAME_DAY_CATCHUP": "true",
                "SAME_DAY_CATCHUP_CUTOFF_MINUTES": "1020",
            },
            clear=False,
        ):
            result = auto_schedule.claim_due_slot(8, now=now)
        self.assertEqual(result["should_run"], "false")
        self.assertEqual(result["reason"], "no_due_slot")


if __name__ == "__main__":
    unittest.main()
