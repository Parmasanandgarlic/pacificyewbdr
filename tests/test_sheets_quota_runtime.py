from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import outreach_compliance as compliance
import sheets_quota_runtime as quota


class FakeWorksheet:
    def __init__(self) -> None:
        self.rows: list[list[str]] = []

    def append_row(self, row) -> None:
        self.rows.append(list(row))


class SheetsQuotaRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        quota._ONE_TOUCH_WORKSHEET = None
        quota._SENT_LEDGER_WORKSHEET = None
        quota._DNC_WORKSHEET = None
        quota._LEADS_BY_EMAIL = None

    def test_one_touch_worksheet_handle_is_reused(self) -> None:
        worksheet = FakeWorksheet()
        original = Mock(return_value=worksheet)
        quota._ORIGINAL_ENSURE_ONE_TOUCH = original

        self.assertIs(quota._cached_one_touch_worksheet(), worksheet)
        self.assertIs(quota._cached_one_touch_worksheet(), worksheet)
        original.assert_called_once_with()

    def test_one_touch_append_defers_verification_to_strict_wrapper(self) -> None:
        worksheet = FakeWorksheet()
        compliance._one_touch_cache = {
            "emails": set(),
            "websites": set(),
            "domains": set(),
            "names": set(),
        }
        lead = {
            "website": "https://exampleclinic.ca",
            "source_url": "https://exampleclinic.ca/contact",
            "consent_evidence_hash": "a" * 64,
            "run_id": "run-1",
        }

        with (
            patch.object(quota, "_cached_find_lead", return_value=lead),
            patch.object(quota, "_cached_one_touch_worksheet", return_value=worksheet),
            patch.object(compliance, "_one_touch_match", return_value=(False, "")),
            patch.object(compliance, "_load_one_touch_keys") as redundant_read,
        ):
            quota._append_one_touch_without_immediate_reread(
                "info@exampleclinic.ca",
                "Example Clinic",
                "Reducing intake admin",
                "production",
            )

        self.assertEqual(len(worksheet.rows), 1)
        redundant_read.assert_not_called()
        self.assertIn("info@exampleclinic.ca", compliance._one_touch_cache["emails"])
        self.assertIn("exampleclinic.ca", compliance._one_touch_cache["domains"])

    def test_stream_append_updates_existing_lead_cache_without_reread(self) -> None:
        quota._LEADS_BY_EMAIL = {}
        quota._ORIGINAL_APPEND_STREAM_ROWS = Mock(return_value=1)
        row = {
            "email": "office@example.ca",
            "website": "https://example.ca",
            "business_name": "Example",
        }

        appended = quota._append_stream_rows([row])

        self.assertEqual(appended, 1)
        self.assertEqual(quota._LEADS_BY_EMAIL["office@example.ca"]["business_name"], "Example")
        quota._ORIGINAL_APPEND_STREAM_ROWS.assert_called_once_with([row])


if __name__ == "__main__":
    unittest.main()
