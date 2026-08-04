import os
import unittest
from unittest.mock import Mock, patch

import growth_engine as growth


class GrowthEngineTests(unittest.TestCase):
    def test_daily_query_slots_do_not_overlap(self):
        slices = {
            slot: set(growth.queries_for_slot(slot, day_of_year=216, query_count=24))
            for slot in ("overnight", "morning", "midday", "afternoon")
        }
        for left_name, left in slices.items():
            self.assertEqual(len(left), 24)
            for right_name, right in slices.items():
                if left_name < right_name:
                    self.assertTrue(left.isdisjoint(right), f"{left_name} overlaps {right_name}")

    def test_email_evidence_uses_visible_same_domain_address(self):
        html = """
        <html><body><h1>North Shore Clinic</h1>
        <p>Appointments and insurance billing.</p>
        <a href="mailto:reception@northshoreclinic.ca">Email reception</a>
        <script>const placeholder = 'hello@businessname.com';</script>
        </body></html>
        """
        response = Mock()
        response.url = "https://northshoreclinic.ca/contact"
        response.text = html
        response.headers = {"content-type": "text/html"}
        response.raise_for_status = Mock()
        with patch.object(growth.requests, "get", return_value=response), \
             patch.object(growth.legacy, "robots_allows", return_value=True):
            result = growth.find_public_business_email("https://northshoreclinic.ca")
        self.assertEqual(result.email, "reception@northshoreclinic.ca")
        self.assertEqual(result.source_url, "https://northshoreclinic.ca/contact")
        self.assertRegex(result.evidence_hash, r"^[a-f0-9]{64}$")
        self.assertNotIn("businessname.com", result.excerpt)

    def test_no_solicitation_page_is_not_eligible(self):
        html = """
        <html><body><p>Contact us at info@strictbusiness.ca.</p>
        <p>No unsolicited marketing emails or sales pitches.</p></body></html>
        """
        response = Mock()
        response.url = "https://strictbusiness.ca/contact"
        response.text = html
        response.headers = {"content-type": "text/html"}
        response.raise_for_status = Mock()
        with patch.object(growth.requests, "get", return_value=response), \
             patch.object(growth.legacy, "robots_allows", return_value=True):
            result = growth.find_public_business_email("https://strictbusiness.ca")
        self.assertFalse(result.email)
        self.assertTrue(result.restricted)

    def test_discovery_does_not_call_llm_without_public_email(self):
        metrics = growth.RunMetrics(
            run_id="test-run",
            run_slot="morning",
            started_at="2026-08-04T15:00:00+00:00",
        )
        business = {"title": "No Email Co", "website": "https://noemail.example"}
        with patch.object(growth, "queries_for_slot", return_value=["one query"]), \
             patch.object(growth.legacy, "discover_businesses", return_value=[business]), \
             patch.object(growth.legacy, "is_directory", return_value=False), \
             patch.object(growth, "load_existing_universe", return_value={"emails": set(), "websites": set(), "names": set()}), \
             patch.object(growth, "find_public_business_email", return_value=growth.EmailEvidence(reason="none")), \
             patch.object(growth, "draft_for_business") as draft_mock, \
             patch.object(growth.legacy, "insert_leads_batch") as insert_mock:
            added = growth.discover_growth_leads(metrics, qualified_target=1)
        self.assertEqual(added, 0)
        self.assertEqual(metrics.no_public_business_email, 1)
        draft_mock.assert_not_called()
        insert_mock.assert_not_called()

    def test_main_sends_existing_queue_before_discovery(self):
        order = []

        def send(limit=None):
            order.append(f"send:{limit}")
            return 1

        def discover(metrics, qualified_target):
            order.append(f"discover:{qualified_target}")
            return 1

        with patch.dict(os.environ, {"SEND_LIMIT": "2", "BDR_RUN_SLOT": "morning"}, clear=False), \
             patch.object(growth, "install"), \
             patch.object(growth, "scan_hard_bounces", return_value=0), \
             patch.object(growth.legacy, "scan_unsubscribes"), \
             patch.object(growth.legacy, "preflight_checks", return_value=True), \
             patch.object(growth, "quarantine_legacy_approvals", return_value=0), \
             patch.object(growth, "approve_growth_drafts", return_value=0), \
             patch.object(growth.legacy, "send_approved", side_effect=send), \
             patch.object(growth, "discover_growth_leads", side_effect=discover), \
             patch.object(growth, "record_metrics"):
            growth.main()

        self.assertEqual(order[0], "send:2")
        self.assertTrue(order[1].startswith("discover:"))
        self.assertEqual(order[2], "send:1")

    def test_offer_routing_is_deterministic(self):
        self.assertEqual(growth.route_offer("AUTOMATION_SIGNALS: online booking"), "booking_and_no_show")
        self.assertEqual(growth.route_offer("AUTOMATION_SIGNALS: quote or estimate intake"), "lead_response_and_estimates")
        self.assertEqual(growth.route_offer("AUTOMATION_SIGNALS: insurance direct billing"), "intake_and_billing_admin")


if __name__ == "__main__":
    unittest.main()
