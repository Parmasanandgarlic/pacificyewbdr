import unittest
from datetime import datetime
from unittest.mock import Mock, patch
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

    def test_all_four_slots_are_due_at_first_scheduled_heartbeat(self):
        heartbeat_times = {
            "morning": (8, 13),
            "late_morning": (10, 13),
            "midday": (12, 13),
            "afternoon": (14, 13),
        }
        for slot, (hour, minute) in heartbeat_times.items():
            with self.subTest(slot=slot):
                due, reason = schedule_control.slot_is_due(
                    slot,
                    datetime(2026, 8, 6, hour, minute, tzinfo=PACIFIC),
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

    def test_sheet_retry_recovers_after_transient_quota_failures(self):
        operation = Mock(side_effect=[RuntimeError("429 quota"), RuntimeError("429 quota"), "ok"])
        with patch.object(schedule_control.time, "sleep") as sleep:
            result = schedule_control._sheet_retry("test read", operation)
        self.assertEqual(result, "ok")
        self.assertEqual(operation.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_sheet_retry_fails_closed_after_all_attempts(self):
        operation = Mock(side_effect=RuntimeError("429 quota"))
        with patch.object(schedule_control.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "failed after 3 attempts"):
                schedule_control._sheet_retry("test read", operation, attempts=3)
        self.assertEqual(operation.call_count, 3)

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

    def _claim_with_attempts(self, attempts, ledger_count, run_cap=8):
        worksheet = Mock()
        with patch.object(schedule_control, "_ensure_control_sheet", return_value=worksheet), \
             patch.object(schedule_control, "_records", return_value=(schedule_control.CONTROL_HEADERS, attempts)), \
             patch.object(schedule_control, "_sent_ledger_count", return_value=ledger_count), \
             patch.dict("os.environ", {"GITHUB_RUN_ID": "999"}, clear=False):
            result = schedule_control.claim_slot(
                "late_morning",
                run_cap,
                datetime(2026, 8, 5, 10, 43, tzinfo=PACIFIC),
            )
        return result, worksheet

    def test_successful_zero_send_attempt_does_not_close_slot(self):
        attempts = [{
            "local_date": "2026-08-05",
            "run_slot": "late_morning",
            "status": "COMPLETED",
            "sent_ledger_count_at_claim": "400",
            "sent_count": "0",
        }]
        result, worksheet = self._claim_with_attempts(attempts, 400)
        self.assertEqual(result["should_run"], "true")
        self.assertEqual(result["send_limit"], "8")
        worksheet.append_row.assert_called_once()

    def test_successful_partial_attempt_retries_only_unsent_capacity(self):
        attempts = [{
            "local_date": "2026-08-05",
            "run_slot": "late_morning",
            "status": "COMPLETED",
            "sent_ledger_count_at_claim": "500",
            "sent_count": "3",
        }]
        result, _worksheet = self._claim_with_attempts(attempts, 503)
        self.assertEqual(result["should_run"], "true")
        self.assertEqual(result["send_limit"], "5")

    def test_existing_attempt_id_is_reused_without_duplicate_append(self):
        attempts = [{
            "local_date": "2026-08-05",
            "run_slot": "late_morning",
            "attempt_id": "2026-08-05-late_morning-999",
            "status": "FAILED",
            "sent_ledger_count_at_claim": "700",
            "sent_count": "2",
        }]
        result, worksheet = self._claim_with_attempts(attempts, 702)
        self.assertEqual(result["should_run"], "true")
        self.assertEqual(result["send_limit"], "6")
        self.assertEqual(result["attempt_id"], "2026-08-05-late_morning-999")
        self.assertEqual(result["reason"], "slot_claim_recovered")
        worksheet.append_row.assert_not_called()

    def test_completed_attempt_blocks_only_after_ledger_reaches_slot_cap(self):
        attempts = [{
            "local_date": "2026-08-05",
            "run_slot": "late_morning",
            "status": "COMPLETED",
            "sent_ledger_count_at_claim": "600",
            "sent_count": "8",
        }]
        result, worksheet = self._claim_with_attempts(attempts, 608)
        self.assertEqual(result["should_run"], "false")
        self.assertEqual(result["reason"], "slot_cap_already_reached")
        worksheet.append_row.assert_called_once()

    def test_finish_slot_retries_idempotent_finalization_update(self):
        worksheet = Mock()
        worksheet.update_cells.side_effect = [RuntimeError("429 quota"), None]
        records = [{
            "_row": "2",
            "attempt_id": "attempt-1",
            "sent_ledger_count_at_claim": "800",
        }]
        with patch.object(schedule_control, "_ensure_control_sheet", return_value=worksheet), \
             patch.object(schedule_control, "_records", return_value=(schedule_control.CONTROL_HEADERS, records)), \
             patch.object(schedule_control, "_sent_ledger_count", return_value=803), \
             patch.object(schedule_control.time, "sleep"):
            sent = schedule_control.finish_slot("attempt-1", True)
        self.assertEqual(sent, 3)
        self.assertEqual(worksheet.update_cells.call_count, 2)


if __name__ == "__main__":
    unittest.main()
