import unittest
from unittest.mock import Mock, patch

import run_reliability


class RunReliabilityTests(unittest.TestCase):
    def test_strict_sent_ledger_rejects_missing_columns(self):
        worksheet = Mock()
        worksheet.get_all_values.return_value = [["email", "subject"]]
        with patch.object(run_reliability.legacy, "_ensure_ledger", return_value=worksheet), \
             patch.object(run_reliability.legacy, "_sheets_throttle"):
            with self.assertRaisesRegex(RuntimeError, "missing columns"):
                run_reliability.strict_sent_ledger_emails()

    def test_strict_dnc_rejects_unreadable_sheet(self):
        worksheet = Mock()
        worksheet.get_all_values.return_value = []
        with patch.object(run_reliability.legacy, "get_dnc_worksheet", return_value=worksheet), \
             patch.object(run_reliability.legacy, "_sheets_throttle"):
            with self.assertRaisesRegex(RuntimeError, "unreadable"):
                run_reliability.strict_dnc_set()

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
