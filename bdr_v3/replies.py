from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .models import (
    InboundReply,
    Opportunity,
    OpportunityStage,
    ReplyAction,
    ReplyAnalysis,
    ReplyIntent,
)
from .policies import classify_reply, is_business_email, normalize_email
from .repository import BdrRepository


class ReplyResponder(Protocol):
    def send_reply(self, *, to_email: str, subject: str, body: str, in_reply_to: str) -> str: ...


@dataclass(frozen=True, slots=True)
class ReplyPolicy:
    booking_url: str = ""
    auto_confirm_unsubscribe: bool = False
    auto_send_booking_link: bool = False
    suppress_not_interested: bool = True
    nurture_delay_days: int = 90


@dataclass(frozen=True, slots=True)
class ReplyProcessResult:
    reply_id: object
    analysis: ReplyAnalysis
    opportunity_id: object | None
    auto_response_id: str = ""
    status: str = "processed"


class ReplyProcessor:
    """Convert mailbox events into suppression, opportunity, and review actions."""

    def __init__(
        self,
        repository: BdrRepository,
        *,
        policy: ReplyPolicy | None = None,
        responder: ReplyResponder | None = None,
    ) -> None:
        self.repository = repository
        self.policy = policy or ReplyPolicy()
        self.responder = responder

    def process(self, reply: InboundReply) -> ReplyProcessResult:
        contact_context = self.repository.get_contact_context_by_email(reply.sender_email)
        if contact_context is None:
            self.repository.audit(
                "unmatched_reply",
                "reply",
                None,
                {
                    "sender_email": normalize_email(reply.sender_email),
                    "provider_message_id": reply.provider_message_id,
                },
            )
            return ReplyProcessResult(
                reply_id=None,
                analysis=ReplyAnalysis(
                    intent=ReplyIntent.AMBIGUOUS,
                    confidence=0.0,
                    summary="Reply sender does not match a known contact.",
                    action=ReplyAction.ESCALATE,
                ),
                opportunity_id=None,
                status="unmatched",
            )

        account_id, contact_id, enrollment_id, route = contact_context
        analysis = classify_reply(reply.subject, reply.body_text, booking_url=self.policy.booking_url)

        if enrollment_id is not None:
            self.repository.pause_enrollment(enrollment_id, f"Inbound reply: {analysis.intent.value}")

        reply_id = self.repository.record_reply(reply, analysis, contact_id, enrollment_id)
        opportunity_id = None
        auto_response_id = ""

        if analysis.intent == ReplyIntent.UNSUBSCRIBE:
            self.repository.suppress(reply.sender_email, "unsubscribe", "reply_processor")
            if enrollment_id is not None:
                self.repository.stop_enrollment(enrollment_id, "unsubscribe")
            if self.policy.auto_confirm_unsubscribe and self.responder and analysis.draft_response:
                auto_response_id = self.responder.send_reply(
                    to_email=reply.sender_email,
                    subject=f"Re: {reply.subject}",
                    body=analysis.draft_response,
                    in_reply_to=reply.provider_message_id,
                )

        elif analysis.intent == ReplyIntent.BOUNCE:
            self.repository.suppress(reply.sender_email, "hard_bounce", "reply_processor")
            if enrollment_id is not None:
                self.repository.stop_enrollment(enrollment_id, "hard_bounce")

        elif analysis.intent == ReplyIntent.NOT_INTERESTED:
            if self.policy.suppress_not_interested:
                self.repository.suppress(reply.sender_email, "not_interested", "reply_processor")
            if enrollment_id is not None:
                self.repository.stop_enrollment(enrollment_id, "not_interested")

        elif analysis.intent in (ReplyIntent.POSITIVE_INTEREST, ReplyIntent.MEETING_REQUEST):
            if route is None:
                raise RuntimeError("Positive reply has no offer-routing decision")
            opportunity = Opportunity(
                account_id=account_id,
                contact_id=contact_id,
                offer=route.offer,
                stage=(
                    OpportunityStage.MEETING_REQUESTED
                    if analysis.intent == ReplyIntent.MEETING_REQUEST
                    else OpportunityStage.QUALIFIED
                ),
                source_reply_id=reply_id,
                summary=analysis.summary,
                next_action=(
                    "Confirm a meeting time and prepare the account brief."
                    if analysis.intent == ReplyIntent.MEETING_REQUEST
                    else "Review the reply and send a tailored workflow outline."
                ),
                next_action_at=datetime.now(timezone.utc),
            )
            opportunity_id = self.repository.create_opportunity(opportunity)
            if (
                analysis.intent == ReplyIntent.MEETING_REQUEST
                and self.policy.auto_send_booking_link
                and self.responder
                and analysis.draft_response
            ):
                auto_response_id = self.responder.send_reply(
                    to_email=reply.sender_email,
                    subject=f"Re: {reply.subject}",
                    body=analysis.draft_response,
                    in_reply_to=reply.provider_message_id,
                )

        elif analysis.intent == ReplyIntent.NOT_NOW and route is not None:
            opportunity_id = self.repository.create_opportunity(
                Opportunity(
                    account_id=account_id,
                    contact_id=contact_id,
                    offer=route.offer,
                    stage=OpportunityStage.NURTURE,
                    source_reply_id=reply_id,
                    summary=analysis.summary,
                    next_action="Review before any future contact; do not re-enroll automatically.",
                    next_action_at=datetime.now(timezone.utc) + timedelta(days=self.policy.nurture_delay_days),
                )
            )

        elif analysis.intent == ReplyIntent.REFERRAL and analysis.referred_email:
            self.repository.audit(
                "referral_received",
                "reply",
                reply_id,
                {
                    "referred_email": analysis.referred_email,
                    "is_business_email": is_business_email(analysis.referred_email),
                    "requires_manual_consent_review": True,
                },
            )

        self.repository.audit(
            "reply_processed",
            "reply",
            reply_id,
            {
                "intent": analysis.intent.value,
                "action": analysis.action.value,
                "confidence": analysis.confidence,
                "opportunity_id": str(opportunity_id) if opportunity_id else None,
                "auto_response_sent": bool(auto_response_id),
            },
        )
        return ReplyProcessResult(reply_id, analysis, opportunity_id, auto_response_id)
