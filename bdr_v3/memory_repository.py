from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

from .models import (
    ClaimedTouch,
    Contact,
    DispatchContext,
    EnrollmentStatus,
    Evidence,
    InboundReply,
    Opportunity,
    OutcomeEvent,
    ProviderReceipt,
    ReplyAnalysis,
    RouteDecision,
    Scorecard,
    SequencePlan,
    TouchStatus,
    VerifiedAccount,
)
from .policies import make_idempotency_key, next_business_send_time, normalize_email
from .repository import AccountRecord, BdrRepository, ContactRecord, EnrollmentRecord, TouchRecord


class MemoryRepository(BdrRepository):
    """Deterministic repository used by tests and local dry-runs."""

    def __init__(self) -> None:
        self.accounts: dict[UUID, AccountRecord] = {}
        self.account_by_domain: dict[str, UUID] = {}
        self.contacts: dict[UUID, ContactRecord] = {}
        self.contact_by_email: dict[str, UUID] = {}
        self.evidence: dict[UUID, list[Evidence]] = defaultdict(list)
        self.enrollments: dict[UUID, EnrollmentRecord] = {}
        self.touches: dict[UUID, TouchRecord] = {}
        self.messages: dict[UUID, dict[str, Any]] = {}
        self.message_by_idempotency: dict[str, UUID] = {}
        self.suppressions: dict[str, dict[str, str]] = {}
        self.replies: dict[UUID, dict[str, Any]] = {}
        self.opportunities: dict[UUID, Opportunity] = {}
        self.audit_events: list[dict[str, Any]] = []
        self.outcomes: list[OutcomeEvent] = []
        self.mailboxes: dict[UUID, dict[str, Any]] = {}
        self.campaigns: dict[UUID, dict[str, Any]] = {}

    def add_mailbox(self, mailbox_id: UUID, *, enabled: bool = True, daily_limit: int = 24) -> None:
        self.mailboxes[mailbox_id] = {"enabled": enabled, "daily_limit": daily_limit}

    def add_campaign(self, campaign_id: UUID, *, name: str = "Default") -> None:
        self.campaigns[campaign_id] = {"name": name}

    def upsert_account(self, account: VerifiedAccount) -> UUID:
        account_id = self.account_by_domain.get(account.domain)
        if account_id is None:
            account_id = uuid4()
            self.account_by_domain[account.domain] = account_id
        existing = self.accounts.get(account_id)
        self.accounts[account_id] = AccountRecord(
            id=account_id,
            verified=account,
            scorecard=existing.scorecard if existing else None,
            route=existing.route if existing else None,
        )
        return account_id

    def upsert_contact(self, account_id: UUID, contact: Contact) -> UUID:
        normalized = normalize_email(contact.email)
        contact_id = self.contact_by_email.get(normalized)
        if contact_id is None:
            contact_id = uuid4()
            self.contact_by_email[normalized] = contact_id
        self.contacts[contact_id] = ContactRecord(contact_id, account_id, replace(contact, email=normalized))
        return contact_id

    def replace_evidence(self, account_id: UUID, evidence: Sequence[Evidence]) -> None:
        self.evidence[account_id] = list(evidence)

    def save_scorecard(self, account_id: UUID, scorecard: Scorecard) -> None:
        record = self.accounts[account_id]
        self.accounts[account_id] = replace(record, scorecard=scorecard)

    def save_route(self, account_id: UUID, route: RouteDecision) -> None:
        record = self.accounts[account_id]
        self.accounts[account_id] = replace(record, route=route)

    def create_enrollment(
        self,
        *,
        account_id: UUID,
        contact_id: UUID,
        mailbox_id: UUID,
        campaign_id: UUID,
        plan: SequencePlan,
        start_at: datetime,
    ) -> UUID:
        for existing in self.enrollments.values():
            if (
                existing.contact_id == contact_id
                and existing.campaign_id == campaign_id
                and existing.status in (EnrollmentStatus.ACTIVE, EnrollmentStatus.PAUSED)
            ):
                return existing.id
        enrollment_id = uuid4()
        self.enrollments[enrollment_id] = EnrollmentRecord(
            id=enrollment_id,
            campaign_id=campaign_id,
            account_id=account_id,
            contact_id=contact_id,
            mailbox_id=mailbox_id,
            status=EnrollmentStatus.ACTIVE,
            offer=plan.offer.value,
        )
        for step in plan.steps:
            step_id = uuid4()
            touch_id = uuid4()
            idempotency_key = make_idempotency_key(campaign_id, contact_id, step.position)
            claimed = ClaimedTouch(
                touch_id=touch_id,
                enrollment_id=enrollment_id,
                campaign_id=campaign_id,
                account_id=account_id,
                contact_id=contact_id,
                mailbox_id=mailbox_id,
                sequence_step_id=step_id,
                step_position=step.position,
                subject=step.subject_template,
                body=step.body_template,
                requires_approval=step.requires_approval,
                approved_at=None,
                idempotency_key=idempotency_key,
            )
            self.touches[touch_id] = TouchRecord(
                claimed=claimed,
                scheduled_for=next_business_send_time(start_at, step.delay_days, plan.timezone),
                status=TouchStatus.SCHEDULED,
            )
        return enrollment_id

    def approve_touch(self, touch_id: UUID, approved_by: str = "human") -> None:
        record = self.touches[touch_id]
        self.touches[touch_id] = replace(
            record,
            claimed=replace(record.claimed, approved_at=datetime.now(timezone.utc)),
        )
        self.audit("touch_approved", "touch", touch_id, {"approved_by": approved_by})

    def claim_due_touches(self, *, worker_id: str, limit: int, now: datetime) -> list[ClaimedTouch]:
        due = sorted(
            (
                record
                for record in self.touches.values()
                if record.status == TouchStatus.SCHEDULED
                and record.scheduled_for <= now
                and (not record.claimed.requires_approval or record.claimed.approved_at is not None)
                and self.enrollments[record.claimed.enrollment_id].status == EnrollmentStatus.ACTIVE
            ),
            key=lambda item: (item.scheduled_for, item.claimed.step_position),
        )[:limit]
        claimed: list[ClaimedTouch] = []
        for record in due:
            self.touches[record.claimed.touch_id] = replace(
                record,
                status=TouchStatus.CLAIMED,
                claimed_by=worker_id,
                claimed_at=now,
            )
            claimed.append(record.claimed)
        return claimed

    def get_dispatch_context(self, touch_id: UUID) -> DispatchContext:
        touch_record = self.touches[touch_id]
        touch = touch_record.claimed
        account = self.accounts[touch.account_id]
        contact = self.contacts[touch.contact_id].contact
        mailbox = self.mailboxes.get(touch.mailbox_id, {"enabled": False, "daily_limit": 0})
        today = datetime.now(timezone.utc).date()
        sent_today = sum(
            1
            for message in self.messages.values()
            if message.get("mailbox_id") == touch.mailbox_id
            and message.get("status") == TouchStatus.SENT
            and message.get("accepted_at")
            and message["accepted_at"].date() == today
        )
        conflicting = any(
            enrollment.contact_id == touch.contact_id
            and enrollment.id != touch.enrollment_id
            and enrollment.status == EnrollmentStatus.ACTIVE
            for enrollment in self.enrollments.values()
        )
        if account.scorecard is None:
            raise RuntimeError("scorecard missing for dispatch")
        return DispatchContext(
            touch=touch,
            account_name=account.verified.name,
            account_domain=account.verified.domain,
            contact_email=contact.email,
            consent_type=contact.consent_type,
            consent_source_url=contact.source_url,
            no_contact_statement=contact.no_contact_statement,
            verified_business_email=contact.verified_business_email,
            scorecard=account.scorecard,
            suppressed=normalize_email(contact.email) in self.suppressions,
            mailbox_enabled=bool(mailbox["enabled"]),
            mailbox_daily_limit=int(mailbox["daily_limit"]),
            mailbox_sent_today=sent_today,
            conflicting_active_enrollment=conflicting,
        )

    def reserve_message(self, touch_id: UUID, idempotency_key: str) -> UUID | None:
        if idempotency_key in self.message_by_idempotency:
            return None
        touch_record = self.touches[touch_id]
        if touch_record.status != TouchStatus.CLAIMED:
            return None
        message_id = uuid4()
        self.message_by_idempotency[idempotency_key] = message_id
        self.messages[message_id] = {
            "touch_id": touch_id,
            "mailbox_id": touch_record.claimed.mailbox_id,
            "status": TouchStatus.RESERVED,
            "idempotency_key": idempotency_key,
        }
        self.touches[touch_id] = replace(touch_record, status=TouchStatus.RESERVED)
        return message_id

    def mark_sent(self, touch_id: UUID, message_id: UUID, receipt: ProviderReceipt) -> None:
        self.messages[message_id].update(
            {
                "status": TouchStatus.SENT,
                "provider_message_id": receipt.provider_message_id,
                "accepted_at": receipt.accepted_at,
            }
        )
        self.touches[touch_id] = replace(self.touches[touch_id], status=TouchStatus.SENT)
        enrollment_id = self.touches[touch_id].claimed.enrollment_id
        remaining = any(
            item.claimed.enrollment_id == enrollment_id
            and item.status in (TouchStatus.SCHEDULED, TouchStatus.CLAIMED, TouchStatus.RESERVED)
            for item in self.touches.values()
        )
        if not remaining:
            self.enrollments[enrollment_id] = replace(
                self.enrollments[enrollment_id], status=EnrollmentStatus.COMPLETED
            )

    def mark_failed(self, touch_id: UUID, message_id: UUID | None, reason: str, uncertain: bool) -> None:
        status = TouchStatus.UNCERTAIN if uncertain else TouchStatus.FAILED
        if message_id is not None:
            self.messages[message_id].update({"status": status, "error": reason})
        record = self.touches[touch_id]
        self.touches[touch_id] = replace(record, status=status, last_error=reason)

    def release_touch(self, touch_id: UUID, reason: str) -> None:
        record = self.touches[touch_id]
        if record.status not in (TouchStatus.CLAIMED, TouchStatus.RESERVED):
            return
        self.touches[touch_id] = replace(
            record, status=TouchStatus.SCHEDULED, claimed_by="", claimed_at=None, last_error=reason
        )

    def pause_enrollment(self, enrollment_id: UUID, reason: str) -> None:
        record = self.enrollments[enrollment_id]
        self.enrollments[enrollment_id] = replace(record, status=EnrollmentStatus.PAUSED)
        self.audit("enrollment_paused", "enrollment", enrollment_id, {"reason": reason})

    def stop_enrollment(self, enrollment_id: UUID, reason: str) -> None:
        record = self.enrollments[enrollment_id]
        self.enrollments[enrollment_id] = replace(record, status=EnrollmentStatus.STOPPED)
        for touch_id, touch in list(self.touches.items()):
            if touch.claimed.enrollment_id == enrollment_id and touch.status == TouchStatus.SCHEDULED:
                self.touches[touch_id] = replace(touch, status=TouchStatus.CANCELLED)
        self.audit("enrollment_stopped", "enrollment", enrollment_id, {"reason": reason})

    def suppress(self, email: str, reason: str, source: str) -> None:
        self.suppressions[normalize_email(email)] = {"reason": reason, "source": source}

    def record_reply(
        self,
        reply: InboundReply,
        analysis: ReplyAnalysis,
        contact_id: UUID,
        enrollment_id: UUID | None,
    ) -> UUID:
        reply_id = uuid4()
        self.replies[reply_id] = {
            "reply": reply,
            "analysis": analysis,
            "contact_id": contact_id,
            "enrollment_id": enrollment_id,
        }
        return reply_id

    def create_opportunity(self, opportunity: Opportunity) -> UUID:
        opportunity_id = uuid4()
        self.opportunities[opportunity_id] = opportunity
        return opportunity_id

    def get_contact_context_by_email(
        self, email: str
    ) -> tuple[UUID, UUID, UUID | None, RouteDecision | None] | None:
        contact_id = self.contact_by_email.get(normalize_email(email))
        if contact_id is None:
            return None
        contact = self.contacts[contact_id]
        enrollment = next(
            (
                item
                for item in self.enrollments.values()
                if item.contact_id == contact_id
                and item.status in (EnrollmentStatus.ACTIVE, EnrollmentStatus.PAUSED)
            ),
            None,
        )
        route = self.accounts[contact.account_id].route
        return contact.account_id, contact_id, enrollment.id if enrollment else None, route

    def audit(
        self,
        event_type: str,
        entity_type: str,
        entity_id: UUID | None,
        payload: Mapping[str, Any],
    ) -> None:
        self.audit_events.append(
            {
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "payload": dict(payload),
                "occurred_at": datetime.now(timezone.utc),
            }
        )

    def record_outcome(self, event: OutcomeEvent) -> None:
        self.outcomes.append(event)
        self.audit(
            "outcome_recorded",
            "account",
            event.account_id,
            {
                "outcome": event.outcome,
                "value_cents": event.value_cents,
                "campaign_id": str(event.campaign_id) if event.campaign_id else None,
            },
        )
