from __future__ import annotations

from datetime import datetime, timezone

from bdr_v3.delivery import DeliveryUncertain
from bdr_v3.models import (
    AccountCandidate,
    Contact,
    Evidence,
    EvidenceKind,
    ProviderReceipt,
    ResearchPacket,
    VerifiedAccount,
)

NOW = datetime(2026, 8, 3, 20, 0, tzinfo=timezone.utc)


def eligible_packet() -> ResearchPacket:
    account = VerifiedAccount(
        name="North Shore Clinic",
        website="https://northshore.example",
        domain="northshore.example",
        source_url="https://northshore.example",
        location="North Vancouver, BC",
        is_operating_business=True,
        confidence=0.94,
        reason="Website verified.",
    )
    contact = Contact(
        email="owner@northshore.example",
        source_url="https://northshore.example/contact",
        role="Owner and Operations Director",
        name="Alex",
        verified_business_email=True,
        no_contact_statement=False,
        confidence=0.92,
    )
    evidence = (
        Evidence(
            EvidenceKind.COMPANY_IDENTITY,
            "The clinic operates in North Vancouver.",
            account.website,
            "North Vancouver clinic",
            0.95,
            NOW,
        ),
        Evidence(
            EvidenceKind.CONTACT_PUBLICATION,
            "The site publishes owner@northshore.example.",
            contact.source_url,
            contact.email,
            0.95,
            NOW,
        ),
        Evidence(
            EvidenceKind.WORKFLOW,
            "The site offers online appointment requests.",
            account.website,
            "Request an appointment",
            0.90,
            NOW,
        ),
        Evidence(
            EvidenceKind.SERVICE,
            "The clinic provides physiotherapy services.",
            account.website,
            "Physiotherapy",
            0.90,
            NOW,
        ),
        Evidence(
            EvidenceKind.SOFTWARE,
            "The site references Jane App.",
            account.website,
            "Jane App",
            0.80,
            NOW,
        ),
        Evidence(
            EvidenceKind.BUYING_SIGNAL,
            "The clinic is hiring.",
            account.website,
            "Join our team",
            0.80,
            NOW,
        ),
    )
    return ResearchPacket(
        account=account,
        contact=contact,
        evidence=evidence,
        business_problem=(
            "Appointment requests arrive through email and an online form, staff repeatedly copy "
            "intake details, route follow-up, and reconcile booking records"
        ),
        buying_signals=("hiring",),
        systems=("Jane App", "Google Workspace"),
        workflow_channels=("email", "online form"),
    )


class FakeDiscovery:
    def discover(self, query):
        yield AccountCandidate(
            "North Shore Clinic",
            "https://northshore.example",
            "https://search.example/result",
        )


class FakeVerifier:
    def verify(self, candidate):
        return eligible_packet().account


class FakeResearcher:
    def research(self, account):
        packet = eligible_packet()
        return ResearchPacket(
            account=account,
            contact=packet.contact,
            evidence=packet.evidence,
            business_problem=packet.business_problem,
            buying_signals=packet.buying_signals,
            systems=packet.systems,
            workflow_channels=packet.workflow_channels,
        )


class FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, *, to_email, subject, body, idempotency_key):
        self.sent.append((to_email, subject, body, idempotency_key))
        return ProviderReceipt(f"provider:{idempotency_key[:12]}", NOW)


class UncertainSender:
    def send(self, **kwargs):
        raise DeliveryUncertain("SMTP disconnected after DATA; reconcile sent folder")


class FakeResponder:
    def __init__(self):
        self.sent = []

    def send_reply(self, **kwargs):
        self.sent.append(kwargs)
        return "reply-provider-id"
