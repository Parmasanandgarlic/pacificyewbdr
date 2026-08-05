import unittest

import fit_scoring_hotfix as scoring


DOSSIER = """RESEARCH_STATUS: evidence-backed
SOURCE_1: https://clinic.example/appointments
EVIDENCE_1: Patients can request appointments online and complete a new-patient form.
"""


def payload(*, fit_score, qualified, impact, confidence, time_to_value, relevance, risk, route="booking_and_no_show"):
    return {
        "qualified": qualified,
        "fit_score": fit_score,
        "recipient_role": "clinic operations",
        "role_relevance": "The office inbox coordinates appointment and intake requests.",
        "buyer_mode": "operations-focused",
        "dominant_outcome": "reduce_administrative_work",
        "reason": "A published intake workflow supports a narrow automation opportunity.",
        "offer_route": route,
        "opportunities": [{
            "workflow": "appointment request to intake handoff",
            "evidence_url": "https://clinic.example/appointments",
            "evidence_fact": "Patients can request appointments online and complete a new-patient form.",
            "business_value": "Reduce repeated intake handling while preserving staff review.",
            "impact": impact,
            "confidence": confidence,
            "time_to_value": time_to_value,
            "recipient_relevance": relevance,
            "implementation_risk": risk,
        }],
    }


class DeterministicFitScoringTests(unittest.TestCase):
    def test_original_sixty_point_opportunity_boundary_qualifies(self):
        strategy = scoring.deterministic_strategy_from_payload(
            payload(
                fit_score=3,
                qualified=False,
                impact=3,
                confidence=3,
                time_to_value=3,
                relevance=4,
                risk=2,
            ),
            DOSSIER,
            "booking_and_no_show",
        )
        self.assertEqual(strategy.fit_score, 60)
        self.assertTrue(strategy.qualified)
        self.assertIn("minimum evidence score=60", strategy.reason)

    def test_score_below_sixty_remains_blocked(self):
        strategy = scoring.deterministic_strategy_from_payload(
            payload(
                fit_score=99,
                qualified=True,
                impact=3,
                confidence=3,
                time_to_value=3,
                relevance=4,
                risk=3,
            ),
            DOSSIER,
            "booking_and_no_show",
        )
        self.assertEqual(strategy.fit_score, 57)
        self.assertFalse(strategy.qualified)

    def test_low_confidence_cannot_qualify_even_with_high_score(self):
        strategy = scoring.deterministic_strategy_from_payload(
            payload(
                fit_score=99,
                qualified=True,
                impact=5,
                confidence=2,
                time_to_value=5,
                relevance=5,
                risk=0,
            ),
            DOSSIER,
            "booking_and_no_show",
        )
        self.assertGreaterEqual(strategy.fit_score, 60)
        self.assertFalse(strategy.qualified)

    def test_offer_route_mismatch_still_fails_closed(self):
        strategy = scoring.deterministic_strategy_from_payload(
            payload(
                fit_score=95,
                qualified=True,
                impact=5,
                confidence=5,
                time_to_value=5,
                relevance=5,
                risk=0,
                route="operations_workflow_audit",
            ),
            DOSSIER,
            "booking_and_no_show",
        )
        self.assertFalse(strategy.qualified)


if __name__ == "__main__":
    unittest.main()
