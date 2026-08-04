from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping, Sequence
from uuid import UUID


class Offer(StrEnum):
    AI_TEAM_ENABLEMENT = "AI Team Enablement"
    WORKFLOW_AUTOMATION = "Workflow Automation"
    INTAKE_ROUTING = "Intake & Routing System"
    CONNECTED_OPERATIONS = "Connected Operations"
    OUTBOUND_PIPELINE = "Outbound Pipeline System"


class EvidenceKind(StrEnum):
    COMPANY_IDENTITY = "company_identity"
    CONTACT_PUBLICATION = "contact_publication"
    LOCATION = "location"
    SERVICE = "service"
    SOFTWARE = "software"
    WORKFLOW = "workflow"
    BUYING_SIGNAL = "buying_signal"
    NO_CONTACT = "no_contact"


class ReplyIntent(StrEnum):
    POSITIVE_INTEREST = "positive_interest"
    MEETING_REQUEST = "meeting_request"
    QUESTION = "question"
    PRICING_QUESTION = "pricing_question"
    OBJECTION = "objection"
    REFERRAL = "referral"
    NOT_NOW = "not_now"
    OUT_OF_OFFICE = "out_of_office"
    NOT_INTERESTED = "not_interested"
    UNSUBSCRIBE = "unsubscribe"
    BOUNCE = "bounce"
    AUTOMATED_RESPONSE = "automated_response"
    AMBIGUOUS = "ambiguous"


class ReplyAction(StrEnum):
    AUTO_CONFIRM_SUPPRESSION = "auto_confirm_suppression"
    AUTO_SEND_BOOKING_LINK = "auto_send_booking_link"
    DRAFT_FOR_REVIEW = "draft_for_review"
    CREATE_OPPORTUNITY_AND_ESCALATE = "create_opportunity_and_escalate"
    PAUSE_UNTIL_RETURN = "pause_until_return"
    CLOSE_AND_SUPPRESS = "close_and_suppress"
    ESCALATE = "escalate"
    IGNORE_AUTOMATED = "ignore_automated"


class TouchStatus(StrEnum):
    SCHEDULED = "scheduled"
    CLAIMED = "claimed"
    RESERVED = "reserved"
    SENT = "sent"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"


class EnrollmentStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    STOPPED = "stopped"


class OpportunityStage(StrEnum):
    NEW = "new"
    QUALIFIED = "qualified"
    MEETING_REQUESTED = "meeting_requested"
    MEETING_BOOKED = "meeting_booked"
    PROPOSAL = "proposal"
    WON = "won"
    LOST = "lost"
    NURTURE = "nurture"


@dataclass(frozen=True, slots=True)
class AccountCandidate:
    name: str
    website: str
    source_url: str
    location: str = ""
    external_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VerifiedAccount:
    name: str
    website: str
    domain: str
    source_url: str
    location: str
    is_operating_business: bool
    confidence: float
    reason: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Contact:
    email: str
    source_url: str
    role: str = ""
    name: str = ""
    consent_type: str = "IMPLIED_CONSPICUOUS"
    verified_business_email: bool = False
    no_contact_statement: bool = False
    confidence: float = 0.0


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: EvidenceKind
    claim: str
    source_url: str
    excerpt: str
    confidence: float
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class ResearchPacket:
    account: VerifiedAccount
    contact: Contact | None
    evidence: tuple[Evidence, ...]
    business_problem: str
    buying_signals: tuple[str, ...] = ()
    systems: tuple[str, ...] = ()
    workflow_channels: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True, slots=True)
class Scorecard:
    fit: int
    timing: int
    authority: int
    evidence: int
    risk: int
    total: int
    eligible: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RouteDecision:
    offer: Offer
    rationale: str
    confidence: float


@dataclass(frozen=True, slots=True)
class SequenceStep:
    position: int
    delay_days: int
    purpose: str
    subject_template: str
    body_template: str
    requires_approval: bool = True


@dataclass(frozen=True, slots=True)
class SequencePlan:
    name: str
    offer: Offer
    timezone: str
    steps: tuple[SequenceStep, ...]


@dataclass(frozen=True, slots=True)
class ClaimedTouch:
    touch_id: UUID
    enrollment_id: UUID
    campaign_id: UUID
    account_id: UUID
    contact_id: UUID
    mailbox_id: UUID
    sequence_step_id: UUID
    step_position: int
    subject: str
    body: str
    requires_approval: bool
    approved_at: datetime | None
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class DispatchContext:
    touch: ClaimedTouch
    account_name: str
    account_domain: str
    contact_email: str
    consent_type: str
    consent_source_url: str
    no_contact_statement: bool
    verified_business_email: bool
    scorecard: Scorecard
    suppressed: bool
    mailbox_enabled: bool
    mailbox_daily_limit: int
    mailbox_sent_today: int
    conflicting_active_enrollment: bool


@dataclass(frozen=True, slots=True)
class ProviderReceipt:
    provider_message_id: str
    accepted_at: datetime
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DispatchResult:
    touch_id: UUID
    status: TouchStatus
    reason: str
    provider_message_id: str = ""


@dataclass(frozen=True, slots=True)
class InboundReply:
    sender_email: str
    recipient_email: str
    subject: str
    body_text: str
    provider_message_id: str
    received_at: datetime
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReplyAnalysis:
    intent: ReplyIntent
    confidence: float
    summary: str
    action: ReplyAction
    return_at: datetime | None = None
    referred_email: str = ""
    draft_response: str = ""


@dataclass(frozen=True, slots=True)
class Opportunity:
    account_id: UUID
    contact_id: UUID
    offer: Offer
    stage: OpportunityStage
    source_reply_id: UUID | None
    summary: str
    next_action: str
    next_action_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class OutcomeEvent:
    account_id: UUID
    campaign_id: UUID | None
    enrollment_id: UUID | None
    outcome: str
    occurred_at: datetime
    value_cents: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AccountProcessResult:
    account_id: UUID
    contact_id: UUID | None
    scorecard: Scorecard
    route: RouteDecision
    enrollment_id: UUID | None
    status: str


JsonObject = Mapping[str, Any]
JsonSequence = Sequence[JsonObject]
