from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID

from .models import (
    ClaimedTouch, Contact, DispatchContext, EnrollmentStatus, Evidence, InboundReply,
    Opportunity, OutcomeEvent, ProviderReceipt, ReplyAnalysis, RouteDecision, Scorecard,
    SequencePlan, TouchStatus, VerifiedAccount,
)

@dataclass(frozen=True, slots=True)
class AccountRecord:
    id: UUID
    verified: VerifiedAccount
    scorecard: Scorecard | None = None
    route: RouteDecision | None = None


@dataclass(frozen=True, slots=True)
class ContactRecord:
    id: UUID
    account_id: UUID
    contact: Contact


@dataclass(frozen=True, slots=True)
class EnrollmentRecord:
    id: UUID
    campaign_id: UUID
    account_id: UUID
    contact_id: UUID
    mailbox_id: UUID
    status: EnrollmentStatus
    offer: str


@dataclass(frozen=True, slots=True)
class TouchRecord:
    claimed: ClaimedTouch
    scheduled_for: datetime
    status: TouchStatus
    claimed_by: str = ""
    claimed_at: datetime | None = None
    last_error: str = ""


class BdrRepository(Protocol):
    def upsert_account(self, account: VerifiedAccount) -> UUID: ...

    def upsert_contact(self, account_id: UUID, contact: Contact) -> UUID: ...

    def replace_evidence(self, account_id: UUID, evidence: Sequence[Evidence]) -> None: ...

    def save_scorecard(self, account_id: UUID, scorecard: Scorecard) -> None: ...

    def save_route(self, account_id: UUID, route: RouteDecision) -> None: ...

    def create_enrollment(
        self,
        *,
        account_id: UUID,
        contact_id: UUID,
        mailbox_id: UUID,
        campaign_id: UUID,
        plan: SequencePlan,
        start_at: datetime,
    ) -> UUID: ...

    def approve_touch(self, touch_id: UUID, approved_by: str) -> None: ...

    def claim_due_touches(self, *, worker_id: str, limit: int, now: datetime) -> list[ClaimedTouch]: ...

    def get_dispatch_context(self, touch_id: UUID) -> DispatchContext: ...

    def reserve_message(self, touch_id: UUID, idempotency_key: str) -> UUID | None: ...

    def mark_sent(self, touch_id: UUID, message_id: UUID, receipt: ProviderReceipt) -> None: ...

    def mark_failed(self, touch_id: UUID, message_id: UUID | None, reason: str, uncertain: bool) -> None: ...

    def release_touch(self, touch_id: UUID, reason: str) -> None: ...

    def pause_enrollment(self, enrollment_id: UUID, reason: str) -> None: ...

    def stop_enrollment(self, enrollment_id: UUID, reason: str) -> None: ...

    def suppress(self, email: str, reason: str, source: str) -> None: ...

    def record_reply(
        self,
        reply: InboundReply,
        analysis: ReplyAnalysis,
        contact_id: UUID,
        enrollment_id: UUID | None,
    ) -> tuple[UUID, bool]: ...

    def create_opportunity(self, opportunity: Opportunity) -> UUID: ...

    def get_contact_context_by_email(self, email: str) -> tuple[UUID, UUID, UUID | None, RouteDecision | None] | None: ...

    def audit(self, event_type: str, entity_type: str, entity_id: UUID | None, payload: Mapping[str, Any]) -> None: ...

    def record_outcome(self, event: OutcomeEvent) -> None: ...


