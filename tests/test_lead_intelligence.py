import unittest
from unittest.mock import patch

from lead_intelligence import (
    QUALIFICATION_THRESHOLD,
    analyze_and_draft,
    build_research_dossier,
    parse_page,
    parse_qualification,
    validate_draft,
)


class LeadIntelligenceTests(unittest.TestCase):
    def test_parse_page_extracts_operational_signals(self):
        html = """
        <html><head><title>North Shore Physio</title>
        <meta name="description" content="Physiotherapy and RMT in North Vancouver">
        </head><body><h1>Book an appointment online</h1>
        <form></form><a href="/services">Our treatments</a>
        <script src="https://janeapp.com/widget.js"></script></body></html>
        """
        page = parse_page("https://example.com", html)
        self.assertEqual(page.title, "North Shore Physio")
        self.assertIn("online booking", page.workflow_signals)
        self.assertIn("Jane clinic software", page.technology)
        self.assertEqual(page.forms, 1)

    @patch("lead_intelligence._fetch")
    def test_dossier_crawls_evidence_pages(self, fetch):
        fetch.side_effect = [
            '<h1>Emergency plumbing</h1><a href="/services">Services</a>',
            '<h1>Drain cleaning</h1><p>Request a free estimate.</p>',
            '<h1>Contact</h1><form></form>',
            RuntimeError("missing"),
            RuntimeError("missing"),
            RuntimeError("missing"),
        ]
        dossier = build_research_dossier("https://example.com")
        self.assertIn("RESEARCH_STATUS: evidence-backed", dossier)
        self.assertIn("SOURCE_1: https://example.com/", dossier)
        self.assertIn("quote or estimate intake", dossier)

    def test_qualified_draft_requires_evidence_and_score(self):
        dossier = """RESEARCH_STATUS: evidence-backed
SOURCE_1: https://clinic.example/services
EVIDENCE_1: Online booking for physiotherapy and massage appointments.
"""
        output = """QUALIFIED: Yes
FIT_SCORE: 82
PRIMARY_SIGNAL: online appointment intake and reminders
EVIDENCE_URL: https://clinic.example/services
REASON: Their appointment workflow is a strong automation fit.
SUBJECT: Reducing appointment follow-up admin
BODY:
Your online appointment flow creates a clear place to reduce repetitive follow-up work. We could connect booking confirmations, reminders, and post-visit follow-ups so staff are not moving the same details between systems. That should reduce manual chasing while keeping the team in control. Open to a short walkthrough of what that could look like?
"""
        result = analyze_and_draft(
            {"title": "Example Clinic", "website": "https://clinic.example"},
            dossier,
            lambda _system, _user, _temperature: output,
        )
        qualified, score = parse_qualification(result["qualified"])
        self.assertTrue(qualified)
        self.assertGreaterEqual(score, QUALIFICATION_THRESHOLD)
        self.assertTrue(result["body"])

    def test_unqualified_draft_is_not_sendable(self):
        result = analyze_and_draft(
            {"title": "Thin Site", "website": "https://thin.example"},
            "RESEARCH_STATUS: unavailable",
            lambda _system, _user, _temperature: """QUALIFIED: Yes
FIT_SCORE: 90
PRIMARY_SIGNAL: booking
EVIDENCE_URL: none
REASON: Generic fit.
SUBJECT: Quick idea
BODY:
This is generic. We automate things. It saves time. Want a call?
""",
        )
        qualified, _score = parse_qualification(result["qualified"])
        self.assertFalse(qualified)
        self.assertEqual(result["subject"], "")
        self.assertEqual(result["body"], "")

    def test_quality_gate_rejects_placeholders(self):
        ok, _ = validate_draft(
            "Idea for [Company]",
            "We saw your booking flow. We can automate reminders and follow-ups for your team. This can reduce admin. Open to a quick call?",
        )
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
