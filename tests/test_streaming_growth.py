import os
import unittest
from unittest.mock import Mock, patch

import growth_engine as growth
import streaming_growth as streaming


class StreamingGrowthTests(unittest.TestCase):
    def test_empty_model_response_is_retryable_not_disqualified(self):
        evidence = growth.EmailEvidence(
            email="office@clinic.example",
            source_url="https://clinic.example/contact",
            observed_at="2026-08-04T20:00:00+00:00",
            excerpt="Email office@clinic.example",
            evidence_hash="a" * 64,
            role_hint="operations or administrative inbox",
        )
        dossier = "SOURCE_1: https://clinic.example/services\nWORKFLOW_SIGNALS: online booking"
        with patch.dict(os.environ, {"OPENROUTER_FALLBACK_MODEL": ""}, clear=False), \
             patch.object(streaming.legacy, "_or_chat", return_value=None):
            decision = streaming.draft_with_retry_state(
                {"title": "Clinic", "website": "https://clinic.example"},
                dossier,
                evidence,
            )
        self.assertEqual(decision.status, "NEEDS_RETRY")
        self.assertNotEqual(decision.status, "DISQUALIFIED")

    def test_direct_mailto_fallback_accepts_custom_business_domain(self):
        html = """
        <html><body><h1>Franchise Clinic</h1>
        <a href="mailto:appointments@parenthealth.ca">Book with our office</a>
        </body></html>
        """
        response = Mock()
        response.url = "https://franchiseclinic.ca/contact"
        response.text = html
        response.headers = {"content-type": "text/html"}
        response.raise_for_status = Mock()
        with patch.object(streaming.requests, "get", return_value=response), \
             patch.object(streaming.legacy, "robots_allows", return_value=True):
            evidence = streaming.find_directly_published_fallback_email("https://franchiseclinic.ca")
        self.assertEqual(evidence.email, "appointments@parenthealth.ca")
        self.assertEqual(evidence.source_url, "https://franchiseclinic.ca/contact")
        self.assertRegex(evidence.evidence_hash, r"^[a-f0-9]{64}$")

    def test_direct_mailto_fallback_blocks_noncommercial_role(self):
        html = '<html><body><a href="mailto:privacy@parenthealth.ca">Privacy</a></body></html>'
        response = Mock()
        response.url = "https://franchiseclinic.ca/contact"
        response.text = html
        response.headers = {"content-type": "text/html"}
        response.raise_for_status = Mock()
        with patch.object(streaming.requests, "get", return_value=response), \
             patch.object(streaming.legacy, "robots_allows", return_value=True):
            evidence = streaming.find_directly_published_fallback_email("https://franchiseclinic.ca")
        self.assertFalse(evidence.email)

    def test_microbatch_flush_sends_before_discovery_finishes(self):
        metrics = growth.RunMetrics(
            run_id="run",
            run_slot="manual",
            started_at="2026-08-04T20:00:00+00:00",
        )
        state = streaming.StreamState(
            remaining=5,
            buffered=[{"email": "one@examplebusiness.ca"}, {"email": "two@examplebusiness.ca"}],
            qualified_since_flush=2,
            last_flush=0,
        )
        with patch.dict(os.environ, {"STREAM_BATCH_QUALIFIED": "2"}, clear=False), \
             patch.object(streaming, "append_leads_safely", return_value=2) as append_mock, \
             patch.object(streaming.growth, "approve_growth_drafts", return_value=2), \
             patch.object(streaming.legacy, "send_approved", return_value=2) as send_mock:
            streaming._flush(state, metrics)
        append_mock.assert_called_once()
        send_mock.assert_called_once_with(limit=5)
        self.assertEqual(state.remaining, 3)
        self.assertEqual(metrics.sent_after_discovery, 2)
        self.assertEqual(state.buffered, [])

    def test_append_rows_uses_live_header_order(self):
        worksheet = Mock()
        worksheet.get_all_values.return_value = [["email", "business_name", "status"]]
        with patch.object(streaming.legacy, "get_sheet", return_value=worksheet), \
             patch.object(streaming.legacy, "_reconcile_header", return_value=["email", "business_name", "status"]), \
             patch.object(streaming.legacy, "_sheets_throttle"):
            added = streaming.append_leads_safely([
                {"business_name": "Acme", "email": "info@acme.ca", "status": "DRAFT_READY"}
            ])
        self.assertEqual(added, 1)
        worksheet.append_rows.assert_called_once_with(
            [["info@acme.ca", "Acme", "DRAFT_READY"]],
            value_input_option="USER_ENTERED",
        )


if __name__ == "__main__":
    unittest.main()
