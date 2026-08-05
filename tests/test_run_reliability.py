import os
import unittest
from datetime import datetime
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import run_reliability


PACIFIC = ZoneInfo("America/Vancouver")


class RunReliabilityTests(unittest.TestCase):
    def setUp(self):
        run_reliability._SENT_LEDGER_ROWS_CACHE = None
        run_reliability._DNC_ROWS_CACHE = None
        run_reliability.legacy._LEDGER_CACHE = None
        run_reliability.legacy._BLOCKED_CACHE = None

    def test_strict_sent_ledger_rejects_missing_columns(self):
        worksheet = Mock()
        worksheet.get_all_values.return_value = [["email", "subject"]]
        with patch.object(run_reliability.legacy, "_ensure_ledger", return_value=worksheet), \
             patch.object(run_reliability.legacy, "_sheets_throttle"), \
             patch.object(run_reliability.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "missing columns"):
                run_reliability.strict_sent_ledger_emails()

    def test_strict_sent_ledger_reuses_validated_cache(self):
        run_reliability.legacy._LEDGER_CACHE = {"office@clinic.ca"}
        with patch.object(run_reliability.legacy, "_ensure_ledger") as ensure:
            result = run_reliability.strict_sent_ledger_emails()
        self.assertEqual(result, {"office@clinic.ca"})
        ensure.assert_not_called()

    def test_strict_dnc_rejects_unreadable_sheet(self):
        worksheet = Mock()
        worksheet.get_all_values.return_value = []
        with patch.object(run_reliability.legacy, "get_dnc_worksheet", return_value=worksheet), \
             patch.object(run_reliability.legacy, "_sheets_throttle"), \
             patch.object(run_reliability.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "unreadable"):
                run_reliability.strict_dnc_set()

    def test_value_retry_spans_transient_quota_failures(self):
        calls = []

        def operation():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("429 quota")
            return ["ready"]

        with patch.object(run_reliability.time, "sleep") as sleep:
            result = run_reliability._read_with_retry("ledger", operation, attempts=5)
        self.assertEqual(result, ["ready"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_daily_cap_populates_send_guard_cache_from_same_read(self):
        rows = [
            ["email", "sent_at"],
            ["sent@clinic.ca", "2026-08-05T18:00:00+00:00"],
        ]
        with patch.object(run_reliability, "_sent_ledger_rows", return_value=rows), \
             patch.object(run_reliability, "datetime") as clock, \
             patch.dict(os.environ, {"DAILY_SEND_CAP": "32"}, clear=False):
            clock.now.return_value = datetime(2026, 8, 5, 14, 0, tzinfo=PACIFIC)
            clock.fromisoformat.side_effect = datetime.fromisoformat
            effective = run_reliability.pacific_effective_send_limit(8)
        self.assertEqual(effective, 8)
        self.assertEqual(run_reliability.legacy._LEDGER_CACHE, {"sent@clinic.ca"})

    def test_retry_helper_recovers_after_transient_failures(self):
        calls = []

        def operation():
            calls.append(1)
            return len(calls) == 3

        with patch.object(run_reliability.time, "sleep"):
            self.assertTrue(run_reliability._attempt("test", operation, attempts=3))
        self.assertEqual(len(calls), 3)

    def test_retry_helper_fails_closed_after_all_attempts(self):
        with patch.object(run_reliability.time, "sleep"):
            self.assertFalse(run_reliability._attempt("test", lambda: False, attempts=3))

    def test_recovery_delivery_window_accepts_delayed_heartbeat(self):
        with patch.dict(os.environ, {
            "INITIAL_OUTREACH_ONLY": "true",
            "ALLOW_MANUAL_DELIVERY": "false",
            "HEARTBEAT_RECOVERY_WINDOW_MINUTES": "125",
        }, clear=False):
            ok, reason = run_reliability.delivery_window_ok_at(
                "morning",
                datetime(2026, 8, 5, 9, 54, tzinfo=PACIFIC),
            )
        self.assertTrue(ok, reason)

    def test_recovery_delivery_window_rejects_after_shared_expiry(self):
        with patch.dict(os.environ, {
            "INITIAL_OUTREACH_ONLY": "true",
            "ALLOW_MANUAL_DELIVERY": "false",
            "HEARTBEAT_RECOVERY_WINDOW_MINUTES": "125",
        }, clear=False):
            ok, reason = run_reliability.delivery_window_ok_at(
                "morning",
                datetime(2026, 8, 5, 10, 6, tzinfo=PACIFIC),
            )
        self.assertFalse(ok)
        self.assertIn("outside approved", reason)

    def test_recovery_window_cannot_be_widened_above_code_cap(self):
        with patch.dict(os.environ, {
            "INITIAL_OUTREACH_ONLY": "true",
            "ALLOW_MANUAL_DELIVERY": "false",
            "HEARTBEAT_RECOVERY_WINDOW_MINUTES": "999",
        }, clear=False):
            ok, _reason = run_reliability.delivery_window_ok_at(
                "morning",
                datetime(2026, 8, 5, 10, 6, tzinfo=PACIFIC),
            )
        self.assertFalse(ok)

    def test_empty_sender_secrets_use_required_identity_defaults(self):
        with patch.object(run_reliability.legacy, "SENDER_NAME", ""), \
             patch.object(run_reliability.legacy, "SENDER_INDIVIDUAL", ""), \
             patch.object(run_reliability.legacy, "SENDER_ADDRESS", "123 Main Street"), \
             patch.object(run_reliability.legacy, "SENDER_WEBSITE", ""), \
             patch.object(run_reliability.legacy, "SENDER_PHONE", ""), \
             patch.object(run_reliability.legacy, "REPLY_TO_EMAIL", ""), \
             patch.object(run_reliability.legacy, "GMAIL_USER", "contact@pacificyew.pro"):
            run_reliability.normalize_sender_identity()
            self.assertEqual(run_reliability.legacy.SENDER_NAME, "Pacific Yew Automations")
            self.assertEqual(run_reliability.legacy.SENDER_INDIVIDUAL, "Michael Goulden")
            self.assertEqual(run_reliability.legacy.SENDER_WEBSITE, "https://pacificyew.pro")
            self.assertEqual(run_reliability.legacy.REPLY_TO_EMAIL, "contact@pacificyew.pro")

    def test_sender_identity_fails_closed_without_physical_address(self):
        with patch.object(run_reliability.legacy, "SENDER_NAME", "Pacific Yew Automations"), \
             patch.object(run_reliability.legacy, "SENDER_INDIVIDUAL", "Michael Goulden"), \
             patch.object(run_reliability.legacy, "SENDER_ADDRESS", ""), \
             patch.object(run_reliability.legacy, "SENDER_WEBSITE", "https://pacificyew.pro"), \
             patch.object(run_reliability.legacy, "SENDER_PHONE", ""), \
             patch.object(run_reliability.legacy, "REPLY_TO_EMAIL", "contact@pacificyew.pro"):
            with self.assertRaisesRegex(RuntimeError, "physical sender address"):
                run_reliability.normalize_sender_identity()

    def test_strict_append_requires_both_ledgers_to_persist(self):
        run_reliability._PRIOR_APPEND = lambda *_args, **_kwargs: None
        with patch.object(run_reliability, "strict_sent_ledger_emails", return_value={"office@clinic.ca"}), \
             patch.object(run_reliability.compliance, "_load_one_touch_keys", return_value={
                 "emails": set(), "websites": set(), "domains": set(), "names": set()
             }):
            with self.assertRaisesRegex(RuntimeError, "One Touch Ledger"):
                run_reliability.strict_append_to_ledgers(
                    "office@clinic.ca", "Clinic", "Subject"
                )


if __name__ == "__main__":
    unittest.main()
