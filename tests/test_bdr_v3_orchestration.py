from __future__ import annotations

import unittest
from datetime import datetime, timezone
from uuid import uuid4

from bdr_v3.delivery import GuardedDeliveryService
from bdr_v3.memory_repository import MemoryRepository
from bdr_v3.models import (
    AccountCandidate,
    InboundReply,
    Offer,
    OutcomeEvent,
    ReplyIntent,
    TouchStatus,
)
from bdr_v3.orchestrator import BusinessDevelopmentEmployee, EmployeeConfig
from bdr_v3.replies import ReplyPolicy, ReplyProcessor
from v3_fixtures import (
    FakeDiscovery,
    FakeResearcher,
    FakeResponder,
    FakeSender,
    FakeVerifier,
    NOW,
    UncertainSender,
)


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.repository = MemoryRepository()
        self.mailbox_id = uuid4()
        self.campaign_id = uuid4()
        self.repository.add_mailbox(self.mailbox_id, enabled=True, daily_limit=24)
        self.repository.add_campaign(self.campaign_id)
        self.sender = FakeSender()
        self.delivery = GuardedDeliveryService(self.repository, self.sender)
        self.reply_processor = ReplyProcessor(
            self.repository,
            policy=ReplyPolicy(booking_url="https://cal.example"),
        )
        self.employee = BusinessDevelopmentEmployee(
            repository=self.repository,
            discovery=FakeDiscovery(),
            verifier=FakeVerifier(),
            researcher=FakeResearcher(),
            delivery=self.delivery,
            replies=self.reply_processor,
            config=EmployeeConfig(self.mailbox_id, self.campaign_id),
        )

    def prepare(self):
        result = self.employee.process_candidate(
            AccountCandidate(
                "North Shore Clinic",
                "https://northshore.example",
                "https://search.example",
            ),
            start_at=NOW,
        )
        self.assertEqual(result.status, "enrolled")
        return result

    def first_touch(self):
        return sorted(
            self.repository.touches.values(),
            key=lambda item: item.claimed.step_position,
        )[0]

    def test_full_prepare_loop_persists_score_route_and_sequence(self):
        result = self.prepare()
        self.assertIsNotNone(result.contact_id)
        self.assertIsNotNone(result.enrollment_id)
        self.assertEqual(result.route.offer, Offer.INTAKE_ROUTING)
        self.assertEqual(len(self.repository.touches), 4)
        self.assertTrue(
            all(t.claimed.requires_approval for t in self.repository.touches.values())
        )

    def test_unapproved_touch_is_not_claimed_or_sent(self):
        self.prepare()
        claimed = self.repository.claim_due_touches(
            worker_id="worker",
            limit=1,
            now=datetime(2026, 8, 20, tzinfo=timezone.utc),
        )
        self.assertEqual(claimed, [])
        self.assertEqual(self.sender.sent, [])

    def test_approved_touch_uses_one_guarded_idempotent_path(self):
        self.prepare()
        touch = self.first_touch()
        self.repository.approve_touch(touch.claimed.touch_id, "michael")
        self.repository.claim_due_touches(
            worker_id="worker",
            limit=1,
            now=datetime(2026, 8, 20, tzinfo=timezone.utc),
        )
        result = self.delivery.dispatch_touch(touch.claimed.touch_id)
        self.assertEqual(result.status, TouchStatus.SENT)
        self.assertEqual(len(self.sender.sent), 1)
        duplicate = self.delivery.dispatch_touch(touch.claimed.touch_id)
        self.assertEqual(duplicate.status, TouchStatus.CANCELLED)
        self.assertEqual(len(self.sender.sent), 1)

    def test_suppression_blocks_delivery(self):
        self.prepare()
        touch = self.first_touch()
        self.repository.approve_touch(touch.claimed.touch_id, "michael")
        self.repository.suppress("owner@northshore.example", "unsubscribe", "test")
        self.repository.claim_due_touches(
            worker_id="worker",
            limit=1,
            now=datetime(2026, 8, 20, tzinfo=timezone.utc),
        )
        result = self.delivery.dispatch_touch(touch.claimed.touch_id)
        self.assertEqual(result.status, TouchStatus.FAILED)
        self.assertIn("suppressed", result.reason.lower())

    def test_provider_uncertainty_never_retries_automatically(self):
        self.prepare()
        touch = self.first_touch()
        self.repository.approve_touch(touch.claimed.touch_id, "michael")
        self.repository.claim_due_touches(
            worker_id="worker",
            limit=1,
            now=datetime(2026, 8, 20, tzinfo=timezone.utc),
        )
        service = GuardedDeliveryService(self.repository, UncertainSender())
        result = service.dispatch_touch(touch.claimed.touch_id)
        self.assertEqual(result.status, TouchStatus.UNCERTAIN)
        self.assertEqual(
            self.repository.touches[touch.claimed.touch_id].status,
            TouchStatus.UNCERTAIN,
        )

    def test_positive_reply_pauses_sequence_and_creates_opportunity(self):
        prepared = self.prepare()
        reply = InboundReply(
            sender_email="owner@northshore.example",
            recipient_email="contact@pacificyew.pro",
            subject="Re: workflow idea",
            body_text="Interested. Please send the outline.",
            provider_message_id="reply-1",
            received_at=NOW,
        )
        result = self.employee.process_reply(reply)
        self.assertEqual(result.analysis.intent, ReplyIntent.POSITIVE_INTEREST)
        self.assertIsNotNone(result.opportunity_id)
        self.assertEqual(
            self.repository.enrollments[prepared.enrollment_id].status.value,
            "paused",
        )

    def test_unsubscribe_suppresses_and_stops_sequence(self):
        prepared = self.prepare()
        reply = InboundReply(
            sender_email="owner@northshore.example",
            recipient_email="contact@pacificyew.pro",
            subject="Re",
            body_text="Please unsubscribe and do not contact me again.",
            provider_message_id="reply-2",
            received_at=NOW,
        )
        result = self.employee.process_reply(reply)
        self.assertEqual(result.analysis.intent, ReplyIntent.UNSUBSCRIBE)
        self.assertIn("owner@northshore.example", self.repository.suppressions)
        self.assertEqual(
            self.repository.enrollments[prepared.enrollment_id].status.value,
            "stopped",
        )

    def test_meeting_link_auto_send_requires_explicit_policy(self):
        self.prepare()
        responder = FakeResponder()
        processor = ReplyProcessor(
            self.repository,
            policy=ReplyPolicy(
                booking_url="https://cal.example",
                auto_send_booking_link=True,
            ),
            responder=responder,
        )
        result = processor.process(
            InboundReply(
                sender_email="owner@northshore.example",
                recipient_email="contact@pacificyew.pro",
                subject="Re",
                body_text="Please send your calendar so we can book a call.",
                provider_message_id="reply-3",
                received_at=NOW,
            )
        )
        self.assertEqual(result.analysis.intent, ReplyIntent.MEETING_REQUEST)
        self.assertEqual(len(responder.sent), 1)
        self.assertIn("https://cal.example", responder.sent[0]["body"])

    def test_unknown_reply_is_logged_but_cannot_mutate_pipeline(self):
        result = self.employee.process_reply(
            InboundReply(
                sender_email="unknown@example.org",
                recipient_email="contact@pacificyew.pro",
                subject="Hello",
                body_text="Can you call me?",
                provider_message_id="reply-unknown",
                received_at=NOW,
            )
        )
        self.assertEqual(result.status, "unmatched")
        self.assertEqual(len(self.repository.opportunities), 0)

    def test_outcome_learning_is_auditable(self):
        prepared = self.prepare()
        event = OutcomeEvent(
            account_id=prepared.account_id,
            campaign_id=self.campaign_id,
            enrollment_id=prepared.enrollment_id,
            outcome="won",
            occurred_at=NOW,
            value_cents=250000,
        )
        self.employee.learn_from_outcome(event)
        self.assertEqual(self.repository.outcomes, [event])
        self.assertTrue(
            any(
                item["event_type"] == "outcome_recorded"
                for item in self.repository.audit_events
            )
        )


if __name__ == "__main__":
    unittest.main()
