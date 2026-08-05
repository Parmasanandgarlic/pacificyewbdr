import unittest
from unittest.mock import Mock, patch

import fit_scoring_hotfix as scoring


HEADERS = [
    "email",
    "status",
    "source_url",
    "consent_type",
    "consent_observed_at",
    "consent_evidence_hash",
    "recipient_role",
    "role_relevance",
    "fit_score",
    "primary_signal",
    "research_evidence_url",
    "offer_route",
    "email_subject",
    "email_body",
    "agent_analysis",
]


def row(*, status="NEEDS_REVIEW", score="60", analysis="Deterministic evidence score=60"):
    return [
        "info@clinic.ca",
        status,
        "https://clinic.ca/contact",
        "IMPLIED_CONSPICUOUS",
        "2026-08-05T20:00:00+00:00",
        "a" * 64,
        "clinic operations",
        "The inbox coordinates appointment and intake requests.",
        score,
        "appointment intake",
        "https://clinic.ca/appointments",
        "booking_and_no_show",
        "Appointment intake",
        "Patients can request appointments online. A small intake handoff could organize those requests for staff review. That would reduce repeated administration without changing clinical decisions. Would it help to compare a simple workflow map?",
        analysis,
    ]


class QueueRecoveryTests(unittest.TestCase):
    def _run(self, data_row, *, quality=True):
        worksheet = Mock()
        worksheet.get_all_values.return_value = [HEADERS, data_row]
        with patch.object(scoring.growth.legacy, "get_sheet", return_value=worksheet), \
             patch.object(scoring.growth.legacy, "_sheets_throttle"), \
             patch.object(scoring.growth.legacy, "is_business_email", return_value=True), \
             patch.object(scoring.growth.legacy, "is_blocked", return_value=False), \
             patch.object(scoring.growth.legacy, "_in_sent_ledger", return_value=False), \
             patch.object(scoring.intelligence, "validate_draft", return_value=(quality, "quality")):
            approved = scoring.approve_evidence_ready_drafts()
        return approved, worksheet

    def test_valid_deterministic_review_is_recovered(self):
        approved, worksheet = self._run(row())
        self.assertEqual(approved, 1)
        cells = worksheet.update_cells.call_args.args[0]
        self.assertEqual(cells[0].value, "APPROVED")

    def test_unrelated_manual_review_is_not_auto_promoted(self):
        approved, worksheet = self._run(row(analysis="manual review requested"))
        self.assertEqual(approved, 0)
        worksheet.update_cells.assert_not_called()

    def test_review_below_sixty_remains_blocked(self):
        approved, worksheet = self._run(row(score="59"))
        self.assertEqual(approved, 0)
        worksheet.update_cells.assert_not_called()

    def test_copy_quality_failure_remains_blocked(self):
        approved, worksheet = self._run(row(), quality=False)
        self.assertEqual(approved, 0)
        worksheet.update_cells.assert_not_called()


if __name__ == "__main__":
    unittest.main()
