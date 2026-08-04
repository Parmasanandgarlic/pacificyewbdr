import json
import unittest
from unittest.mock import patch

import email_copy_intelligence as copy
import growth_engine as growth


DOSSIER = """RESEARCH_STATUS: evidence-backed
SOURCE_1: https://clinic.example/appointments
EVIDENCE_1: Patients can request appointments online and complete a new-patient form.
SOURCE_2: https://clinic.example/services
EVIDENCE_2: The clinic offers recurring treatment plans.
"""
EVIDENCE = growth.EmailEvidence(
    email="office@clinic.example",
    source_url="https://clinic.example/contact",
    observed_at="2026-08-04T20:00:00+00:00",
    excerpt="Email office@clinic.example",
    evidence_hash="a" * 64,
    role_hint="operations or administrative inbox",
)


def strategy_payload(opportunities):
    return json.dumps({
        "qualified": True,
        "fit_score": 84,
        "recipient_role": "clinic operations",
        "role_relevance": "The office inbox handles appointment and intake coordination.",
        "buyer_mode": "operations-focused",
        "dominant_outcome": "reduce_administrative_work",
        "reason": "A supported intake workflow has a narrow automation opportunity.",
        "offer_route": "booking_and_no_show",
        "opportunities": opportunities,
    })


GOOD_BODY = (
    "Your appointment page combines online requests with a separate new-patient form. "
    "Pacific Yew could connect those steps so each complete request reaches the office in one consistent record. "
    "That would reduce repeated intake handling while keeping staff in control of exceptions and sensitive details. "
    "Would it help to see the smallest version of that workflow?"
)


class EmailCopyIntelligenceTests(unittest.TestCase):
    def test_opportunity_ranking_is_deterministic(self):
        payload = {
            "qualified": True,
            "fit_score": 85,
            "recipient_role": "operations",
            "role_relevance": "Owns appointment intake.",
            "buyer_mode": "operations-focused",
            "dominant_outcome": "reduce_administrative_work",
            "reason": "supported",
            "offer_route": "booking_and_no_show",
            "opportunities": [
                {
                    "workflow": "low-confidence retention idea",
                    "evidence_url": "https://clinic.example/services",
                    "evidence_fact": "The clinic offers recurring treatment plans.",
                    "business_value": "Support repeat visits.",
                    "impact": 5,
                    "confidence": 1,
                    "time_to_value": 1,
                    "recipient_relevance": 1,
                    "implementation_risk": 5,
                },
                {
                    "workflow": "appointment request to intake handoff",
                    "evidence_url": "https://clinic.example/appointments",
                    "evidence_fact": "Patients can request appointments online and complete a new-patient form.",
                    "business_value": "Reduce repeated intake handling.",
                    "impact": 4,
                    "confidence": 5,
                    "time_to_value": 4,
                    "recipient_relevance": 5,
                    "implementation_risk": 1,
                },
            ],
        }
        strategy = copy._strategy_from_payload(payload, DOSSIER, "booking_and_no_show")
        self.assertTrue(strategy.qualified)
        self.assertEqual(strategy.opportunity.workflow, "appointment request to intake handoff")
        self.assertGreaterEqual(strategy.opportunity.score, 60)

    def test_unsupported_evidence_cannot_qualify(self):
        payload = {
            "qualified": True,
            "fit_score": 90,
            "recipient_role": "operations",
            "role_relevance": "Owns intake.",
            "buyer_mode": "operations-focused",
            "dominant_outcome": "reduce_administrative_work",
            "reason": "unsupported",
            "offer_route": "booking_and_no_show",
            "opportunities": [{
                "workflow": "intake",
                "evidence_url": "https://unverified.example/page",
                "evidence_fact": "Unsupported fact.",
                "business_value": "Save time.",
                "impact": 5,
                "confidence": 5,
                "time_to_value": 5,
                "recipient_relevance": 5,
                "implementation_risk": 0,
            }],
        }
        strategy = copy._strategy_from_payload(payload, DOSSIER, "booking_and_no_show")
        self.assertFalse(strategy.qualified)
        self.assertIsNone(strategy.opportunity)

    def test_generic_copy_is_rejected_by_deterministic_gate(self):
        body = (
            "I came across your website and was impressed by the clinic. "
            "We offer a game-changing AI-powered solution for your team. "
            "It could revolutionize your business and guarantee better results. "
            "Would it help to hear more?"
        )
        valid, issues = copy._deterministic_copy_gate("Quick question", body)
        self.assertFalse(valid)
        self.assertIn("generic cold-email opener", issues)
        self.assertIn("hype or generic AI language", issues)

    def test_end_to_end_returns_ready_only_after_independent_review(self):
        strategy = strategy_payload([{
            "workflow": "appointment request to intake handoff",
            "evidence_url": "https://clinic.example/appointments",
            "evidence_fact": "Patients can request appointments online and complete a new-patient form.",
            "business_value": "Reduce repeated intake handling while preserving staff review.",
            "impact": 4,
            "confidence": 5,
            "time_to_value": 4,
            "recipient_relevance": 5,
            "implementation_risk": 1,
        }])
        draft = json.dumps({"subject": "Appointment intake handoff", "body": GOOD_BODY})
        review = json.dumps({
            "approved": True,
            "personalization": 92,
            "specificity": 91,
            "evidence_fidelity": 98,
            "recipient_relevance": 94,
            "clarity": 93,
            "spam_risk": 6,
            "issues": [],
            "revised_subject": "",
            "revised_body": "",
        })
        with patch.object(copy.growth, "route_offer", return_value="booking_and_no_show"), \
             patch.object(copy, "_model_call", side_effect=[strategy, draft, review]):
            decision = copy.draft_with_retry_state(
                {"title": "Clinic", "website": "https://clinic.example"},
                DOSSIER,
                EVIDENCE,
            )
        self.assertEqual(decision.status, "DRAFT_READY")
        self.assertEqual(decision.subject, "Appointment intake handoff")
        self.assertIn("personalization:92", decision.analysis)

    def test_reviewer_can_repair_a_draft_without_new_facts(self):
        strategy = strategy_payload([{
            "workflow": "appointment request to intake handoff",
            "evidence_url": "https://clinic.example/appointments",
            "evidence_fact": "Patients can request appointments online and complete a new-patient form.",
            "business_value": "Reduce repeated intake handling while preserving staff review.",
            "impact": 4,
            "confidence": 5,
            "time_to_value": 4,
            "recipient_relevance": 5,
            "implementation_risk": 1,
        }])
        bad_draft = json.dumps({
            "subject": "Quick question",
            "body": "I came across your website. We build AI automation. It saves time. Would it help to chat?",
        })
        review = json.dumps({
            "approved": True,
            "personalization": 90,
            "specificity": 90,
            "evidence_fidelity": 96,
            "recipient_relevance": 92,
            "clarity": 92,
            "spam_risk": 8,
            "issues": ["generic opener repaired"],
            "revised_subject": "Appointment intake handoff",
            "revised_body": GOOD_BODY,
        })
        with patch.object(copy.growth, "route_offer", return_value="booking_and_no_show"), \
             patch.object(copy, "_model_call", side_effect=[strategy, bad_draft, review]):
            decision = copy.draft_with_retry_state(
                {"title": "Clinic", "website": "https://clinic.example"},
                DOSSIER,
                EVIDENCE,
            )
        self.assertEqual(decision.status, "DRAFT_READY")
        self.assertEqual(decision.body, GOOD_BODY)


if __name__ == "__main__":
    unittest.main()
