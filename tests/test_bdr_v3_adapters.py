from __future__ import annotations

import smtplib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bdr_v3.adapters import LegacyResearchAdapter, LegacyZohoMailSender
from bdr_v3.delivery import DeliveryUncertain
from bdr_v3.models import EvidenceKind, VerifiedAccount


class _FakeSMTP:
    messages = []

    def __init__(self, *args, **kwargs):
        self.logged_in = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def login(self, username, password):
        self.logged_in = bool(username and password)

    def send_message(self, message):
        self.messages.append(message)


class _DisconnectingSMTP(_FakeSMTP):
    def send_message(self, message):
        raise smtplib.SMTPServerDisconnected("connection closed after DATA")


class AdapterTests(unittest.TestCase):
    def account(self):
        return VerifiedAccount(
            name="Example Clinic",
            website="https://example.test",
            domain="example.test",
            source_url="https://example.test",
            location="Vancouver, BC",
            is_operating_business=True,
            confidence=0.95,
            reason="verified",
        )

    def test_research_preserves_exact_contact_publication_url(self):
        fake = SimpleNamespace(_last_scraped_source_url="")
        fake.scrape_website = lambda url: "Book online. We use Jane App."

        def scrape_email(url):
            fake._last_scraped_source_url = "https://example.test/contact"
            return "hello@example.test"

        fake.scrape_email = scrape_email
        with patch.dict(sys.modules, {"bdr_agent": fake}):
            packet = LegacyResearchAdapter().research(self.account())
        self.assertEqual(packet.contact.source_url, "https://example.test/contact")
        publication = next(
            item
            for item in packet.evidence
            if item.kind == EvidenceKind.CONTACT_PUBLICATION
        )
        self.assertEqual(publication.source_url, "https://example.test/contact")

    def test_smtp_sender_uses_stable_message_identity(self):
        fake = SimpleNamespace(
            GMAIL_USER="contact@pacificyew.pro",
            GMAIL_APP_PASSWORD="secret",
            SENDER_ADDRESS="123 Main Street",
            SENDER_NAME="Pacific Yew Automations",
            REPLY_TO_EMAIL="contact@pacificyew.pro",
            SMTP_HOST="smtp.zoho.com",
            casl_footer=lambda: "\n\n--\nCompliance footer",
        )
        _FakeSMTP.messages = []
        with patch.dict(sys.modules, {"bdr_agent": fake}), patch(
            "bdr_v3.adapters.smtplib.SMTP_SSL", _FakeSMTP
        ):
            receipt = LegacyZohoMailSender().send(
                to_email="owner@example.test",
                subject="Hello\r\nInjected: no",
                body="Workflow note",
                idempotency_key="abc123",
            )
        self.assertEqual(receipt.provider_message_id, "<bdr-abc123@pacificyew.pro>")
        message = _FakeSMTP.messages[0]
        self.assertEqual(message["Message-ID"], "<bdr-abc123@pacificyew.pro>")
        self.assertEqual(message["X-Pacific-Yew-Idempotency-Key"], "abc123")
        self.assertNotIn("\n", message["Subject"])
        self.assertIn("Compliance footer", message.get_content())

    def test_smtp_disconnect_after_submission_is_uncertain(self):
        fake = SimpleNamespace(
            GMAIL_USER="contact@pacificyew.pro",
            GMAIL_APP_PASSWORD="secret",
            SENDER_ADDRESS="123 Main Street",
            SENDER_NAME="Pacific Yew Automations",
            REPLY_TO_EMAIL="contact@pacificyew.pro",
            SMTP_HOST="smtp.zoho.com",
            casl_footer=lambda: "\n\n--\nCompliance footer",
        )
        with patch.dict(sys.modules, {"bdr_agent": fake}), patch(
            "bdr_v3.adapters.smtplib.SMTP_SSL", _DisconnectingSMTP
        ):
            with self.assertRaises(DeliveryUncertain):
                LegacyZohoMailSender().send(
                    to_email="owner@example.test",
                    subject="Hello",
                    body="Workflow note",
                    idempotency_key="abc123",
                )


if __name__ == "__main__":
    unittest.main()
