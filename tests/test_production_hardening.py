import unittest
from unittest.mock import patch

import production_hardening as hardening


class ProductionHardeningTests(unittest.TestCase):
    def test_quoted_footer_does_not_trigger_opt_out(self):
        reply = (
            "Thanks, please send the walkthrough.\n\n"
            "On Tue, Aug 4, 2026 at 10:00 AM Pacific Yew wrote:\n"
            "> To unsubscribe: reply UNSUBSCRIBE."
        )
        self.assertFalse(hardening.is_explicit_opt_out(reply))

    def test_explicit_opt_out_is_detected(self):
        self.assertTrue(hardening.is_explicit_opt_out("UNSUBSCRIBE"))
        self.assertTrue(hardening.is_explicit_opt_out("Please remove me from your list."))
        self.assertFalse(hardening.is_explicit_opt_out("How does unsubscribe handling work?"))

    def test_only_permanent_dsn_is_hard_bounce(self):
        self.assertTrue(hardening.is_hard_bounce("Status: 5.1.1\nDiagnostic-Code: user unknown"))
        self.assertFalse(hardening.is_hard_bounce("Status: 4.2.2\nMailbox temporarily full"))

    def test_failed_recipient_prefers_dsn_field(self):
        text = "Final-Recipient: rfc822; lead@acme.ca\nStatus: 5.1.1\nother@noise.ca"
        result = hardening.extract_failed_recipients(text, {"lead@acme.ca", "other@noise.ca"})
        self.assertEqual(result, ["lead@acme.ca"])

    def test_protected_role_inboxes_are_blocked(self):
        self.assertFalse(hardening.recipient_allowed("privacy@company.ca"))
        self.assertFalse(hardening.recipient_allowed("careers@company.ca"))
        self.assertTrue(hardening.recipient_allowed("reception@company.ca"))

    def test_offer_router_prioritizes_supported_high_value_workflow(self):
        dossier = "online booking appointment insurance direct billing intake form"
        self.assertEqual(hardening.route_offer(dossier), "intake_and_billing_admin")

    def test_manual_query_slice_avoids_scheduled_slices(self):
        count = 24
        day = 216
        manual = set(hardening.manual_safe_queries("manual", day_of_year=day, query_count=count))
        scheduled = set()
        for slot in ("overnight", "morning", "midday", "afternoon"):
            scheduled.update(hardening._ORIGINAL_QUERIES(slot, day_of_year=day, query_count=count))
        self.assertEqual(len(manual), count)
        self.assertTrue(manual.isdisjoint(scheduled))

    def test_delivery_message_has_unsubscribe_and_message_id_headers(self):
        with patch.object(hardening.legacy, "GMAIL_USER", "contact@pacificyew.pro"), \
             patch.object(hardening.legacy, "REPLY_TO_EMAIL", "contact@pacificyew.pro"), \
             patch.object(hardening.legacy, "SENDER_NAME", "Pacific Yew Automations"), \
             patch.object(hardening.legacy, "casl_footer", return_value="\nfooter"):
            message = hardening.build_message("info@acme.ca", "A useful idea", "Body")
        self.assertIn("mailto:contact@pacificyew.pro", message["List-Unsubscribe"])
        self.assertTrue(message["Message-ID"])
        self.assertEqual(message["Reply-To"], "contact@pacificyew.pro")


if __name__ == "__main__":
    unittest.main()
