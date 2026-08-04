import os
import unittest
from unittest.mock import Mock, patch

import growth_engine as growth
import outreach_compliance as compliance


class OutreachComplianceTests(unittest.TestCase):
    def setUp(self):
        compliance._one_touch_cache = None
        compliance._one_touch_error = ""

    @staticmethod
    def _response(html: str, url: str = "https://clinic.ca/contact"):
        response = Mock()
        response.url = url
        response.text = html
        response.headers = {"content-type": "text/html; charset=utf-8"}
        response.raise_for_status = Mock()
        return response

    def test_accepts_direct_first_party_mailto_with_operational_role(self):
        html = '<html><body><h1>Contact our clinic</h1><a href="mailto:office@clinic.ca">Office</a></body></html>'
        with patch.object(compliance.requests, "get", return_value=self._response(html)), \
             patch.object(compliance.legacy, "robots_allows", return_value=True):
            evidence = compliance.strict_first_party_email("https://clinic.ca")

        self.assertEqual(evidence.email, "office@clinic.ca")
        self.assertEqual(evidence.source_url, "https://clinic.ca/contact")
        self.assertEqual(getattr(evidence, "publication_method"), "MAILTO")
        self.assertEqual(getattr(evidence, "publication_first_party"), "TRUE")
        self.assertIn("office@clinic.ca", evidence.excerpt)
        self.assertRegex(evidence.evidence_hash, r"^[a-f0-9]{64}$")

    def test_rejects_cross_domain_address_even_when_published_on_site(self):
        html = '<html><body><a href="mailto:office@parent-company.ca">Office</a></body></html>'
        with patch.object(compliance.requests, "get", return_value=self._response(html)), \
             patch.object(compliance.legacy, "robots_allows", return_value=True):
            evidence = compliance.strict_first_party_email("https://clinic.ca")

        self.assertFalse(evidence.email)
        self.assertIn("first-party", evidence.reason)

    def test_rejects_business_when_no_solicitation_statement_is_present(self):
        html = (
            '<html><body><p>No unsolicited marketing emails.</p>'
            '<a href="mailto:office@clinic.ca">Office</a></body></html>'
        )
        with patch.object(compliance.requests, "get", return_value=self._response(html)), \
             patch.object(compliance.legacy, "robots_allows", return_value=True):
            evidence = compliance.strict_first_party_email("https://clinic.ca")

        self.assertFalse(evidence.email)
        self.assertTrue(evidence.restricted)

    def test_rejects_address_without_commercial_or_operational_role_context(self):
        html = '<html><body><h1>Contact</h1><a href="mailto:john@clinic.ca">Email John</a></body></html>'
        with patch.object(compliance.requests, "get", return_value=self._response(html)), \
             patch.object(compliance.legacy, "robots_allows", return_value=True):
            evidence = compliance.strict_first_party_email("https://clinic.ca")

        self.assertFalse(evidence.email)

    def _lead(self):
        observed_at = "2026-08-04T20:00:00+00:00"
        excerpt = "mailto link: Office | published address: office@clinic.ca"
        rationale = "Published office inbox is associated with commercial or operational business functions."
        evidence_hash = compliance._evidence_hash(
            "office@clinic.ca",
            "https://clinic.ca/contact",
            observed_at,
            excerpt,
            "MAILTO",
            rationale,
        )
        return {
            "business_name": "Clinic",
            "website": "https://clinic.ca",
            "email": "office@clinic.ca",
            "source_url": "https://clinic.ca/contact",
            "consent_type": "IMPLIED_CONSPICUOUS",
            "consent_observed_at": observed_at,
            "consent_evidence_excerpt": excerpt,
            "consent_evidence_hash": evidence_hash,
            "recipient_role": "operations or administrative inbox",
            "role_relevance": "Office staff handle appointment and intake workflows.",
            "publication_method": "MAILTO",
            "publication_page_host": "clinic.ca",
            "publication_first_party": "TRUE",
            "publication_role_rationale": rationale,
            "no_solicitation_checked_at": observed_at,
            "no_solicitation_statement": "NONE_FOUND",
            "initial_outreach_only": "TRUE",
            "commercial_ad_disclosure": "TRUE",
            "compliance_profile": compliance.COMPLIANCE_PROFILE,
        }

    def test_pre_send_accepts_complete_first_party_evidence(self):
        compliance._prior_pre_send = lambda _lead: (True, "ok")
        empty = {"emails": set(), "websites": set(), "domains": set(), "names": set()}
        with patch.object(compliance, "_load_one_touch_keys", return_value=empty):
            ok, reason = compliance._strict_pre_send(self._lead())
        self.assertTrue(ok, reason)

    def test_one_touch_ledger_blocks_second_address_at_same_business(self):
        compliance._prior_pre_send = lambda _lead: (True, "ok")
        contacted = {
            "emails": {"sales@clinic.ca"},
            "websites": {"clinic.ca"},
            "domains": {"clinic.ca"},
            "names": {"clinic"},
        }
        with patch.object(compliance, "_load_one_touch_keys", return_value=contacted):
            ok, reason = compliance._strict_pre_send(self._lead())
        self.assertFalse(ok)
        self.assertIn("already received initial outreach", reason)

    def test_pre_send_rejects_tampered_evidence_hash(self):
        compliance._prior_pre_send = lambda _lead: (True, "ok")
        lead = self._lead()
        lead["consent_evidence_excerpt"] += " changed"
        empty = {"emails": set(), "websites": set(), "domains": set(), "names": set()}
        with patch.object(compliance, "_load_one_touch_keys", return_value=empty):
            ok, reason = compliance._strict_pre_send(lead)
        self.assertFalse(ok)
        self.assertIn("hash mismatch", reason)

    def test_manual_delivery_is_disabled_by_default(self):
        with patch.dict(os.environ, {"BDR_RUN_SLOT": "manual", "ALLOW_MANUAL_DELIVERY": "false"}, clear=False):
            ok, reason = compliance._delivery_window_ok()
        self.assertFalse(ok)
        self.assertIn("manual delivery is disabled", reason)

    def test_footer_contains_ad_identity_address_and_unsubscribe(self):
        with patch.object(compliance.legacy, "SENDER_NAME", "Pacific Yew Automations"), \
             patch.object(compliance.legacy, "SENDER_INDIVIDUAL", "Michael Goulden"), \
             patch.object(compliance.legacy, "SENDER_ADDRESS", "123 Example Street, Surrey, BC"), \
             patch.object(compliance.legacy, "SENDER_WEBSITE", "https://pacificyew.pro"), \
             patch.object(compliance.legacy, "SENDER_PHONE", ""), \
             patch.object(compliance.legacy, "REPLY_TO_EMAIL", "contact@pacificyew.pro"):
            footer = compliance.compliant_footer()
        self.assertIn("commercial advertisement", footer)
        self.assertIn("123 Example Street", footer)
        self.assertIn("UNSUBSCRIBE", footer)
        self.assertIn("contact@pacificyew.pro", footer)

    def test_lead_record_persists_compliance_evidence(self):
        evidence = growth.EmailEvidence(email="office@clinic.ca")
        setattr(evidence, "publication_method", "MAILTO")
        setattr(evidence, "publication_page_host", "clinic.ca")
        setattr(evidence, "publication_first_party", "TRUE")
        setattr(evidence, "publication_role_rationale", "office role")
        setattr(evidence, "no_solicitation_checked_at", "2026-08-04T20:00:00+00:00")
        setattr(evidence, "no_solicitation_statement", "NONE_FOUND")
        compliance._prior_lead_record = lambda *_args, **_kwargs: {"email": evidence.email}

        record = compliance._compliant_lead_record({}, evidence, object(), object())
        self.assertEqual(record["publication_method"], "MAILTO")
        self.assertEqual(record["initial_outreach_only"], "TRUE")
        self.assertEqual(record["commercial_ad_disclosure"], "TRUE")
        self.assertEqual(record["compliance_profile"], compliance.COMPLIANCE_PROFILE)


if __name__ == "__main__":
    unittest.main()
