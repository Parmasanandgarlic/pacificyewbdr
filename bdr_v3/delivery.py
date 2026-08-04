from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .models import DispatchResult, ProviderReceipt, TouchStatus
from .policies import is_business_email
from .repository import BdrRepository


class DeliveryError(RuntimeError):
    """Provider rejected the message before acceptance."""


class DeliveryUncertain(RuntimeError):
    """The provider outcome is unknown; automatic retries are unsafe."""


class MailSender(Protocol):
    def send(self, *, to_email: str, subject: str, body: str, idempotency_key: str) -> ProviderReceipt: ...


@dataclass(frozen=True, slots=True)
class AutonomyPolicy:
    require_approval_for_initial: bool = True
    require_approval_for_followups: bool = True
    minimum_total_score: int = 55
    maximum_risk_score: int = 35

    def approval_required(self, step_position: int, touch_requires_approval: bool) -> bool:
        if touch_requires_approval:
            return True
        if step_position == 1:
            return self.require_approval_for_initial
        return self.require_approval_for_followups


class GuardedDeliveryService:
    """The only permitted v3 path from a scheduled touch to a mail provider.

    Every dispatch re-checks evidence, consent, suppression, account scoring,
    mailbox health, quotas, campaign conflicts, approval, and idempotency. A
    provider timeout after submission is marked UNCERTAIN and is never retried
    automatically.
    """

    def __init__(
        self,
        repository: BdrRepository,
        sender: MailSender,
        *,
        autonomy: AutonomyPolicy | None = None,
    ) -> None:
        self.repository = repository
        self.sender = sender
        self.autonomy = autonomy or AutonomyPolicy()

    def dispatch_touch(self, touch_id) -> DispatchResult:
        context = self.repository.get_dispatch_context(touch_id)
        touch = context.touch

        blocked_reason, temporary = self._blocked_reason(context)
        if blocked_reason:
            if temporary:
                self.repository.release_touch(touch.touch_id, blocked_reason)
                status = TouchStatus.SCHEDULED
                event_type = "touch_deferred"
            else:
                self.repository.mark_failed(touch.touch_id, None, blocked_reason, False)
                status = TouchStatus.FAILED
                event_type = "touch_blocked"
            self.repository.audit(
                event_type,
                "touch",
                touch.touch_id,
                {"reason": blocked_reason, "idempotency_key": touch.idempotency_key},
            )
            return DispatchResult(touch.touch_id, status, blocked_reason)

        message_id = self.repository.reserve_message(touch.touch_id, touch.idempotency_key)
        if message_id is None:
            return DispatchResult(
                touch.touch_id,
                TouchStatus.CANCELLED,
                "An idempotent message reservation already exists or the touch is no longer claimable.",
            )

        try:
            receipt = self.sender.send(
                to_email=context.contact_email,
                subject=touch.subject,
                body=touch.body,
                idempotency_key=touch.idempotency_key,
            )
        except DeliveryUncertain as exc:
            reason = str(exc) or "Provider outcome is uncertain. Manual reconciliation is required."
            self.repository.mark_failed(touch.touch_id, message_id, reason, True)
            self.repository.audit(
                "delivery_uncertain",
                "touch",
                touch.touch_id,
                {"reason": reason, "message_id": str(message_id)},
            )
            return DispatchResult(touch.touch_id, TouchStatus.UNCERTAIN, reason)
        except Exception as exc:
            reason = str(exc) or exc.__class__.__name__
            self.repository.mark_failed(touch.touch_id, message_id, reason, False)
            self.repository.audit(
                "delivery_failed",
                "touch",
                touch.touch_id,
                {"reason": reason, "message_id": str(message_id)},
            )
            return DispatchResult(touch.touch_id, TouchStatus.FAILED, reason)

        self.repository.mark_sent(touch.touch_id, message_id, receipt)
        self.repository.audit(
            "message_sent",
            "touch",
            touch.touch_id,
            {
                "message_id": str(message_id),
                "provider_message_id": receipt.provider_message_id,
                "accepted_at": receipt.accepted_at.isoformat(),
            },
        )
        return DispatchResult(
            touch.touch_id,
            TouchStatus.SENT,
            "Provider accepted the message.",
            receipt.provider_message_id,
        )

    def dispatch_due(self, *, worker_id: str, limit: int, now: datetime | None = None) -> list[DispatchResult]:
        current = now or datetime.now(timezone.utc)
        touches = self.repository.claim_due_touches(worker_id=worker_id, limit=limit, now=current)
        return [self.dispatch_touch(touch.touch_id) for touch in touches]

    def _blocked_reason(self, context) -> tuple[str, bool]:
        touch = context.touch
        score = context.scorecard
        if context.suppressed:
            return "Recipient is suppressed.", False
        if not context.mailbox_enabled:
            return "Mailbox is disabled or unhealthy.", True
        if context.mailbox_sent_today >= context.mailbox_daily_limit:
            return "Mailbox daily limit has been reached.", True
        if context.conflicting_active_enrollment:
            return "Another active campaign already targets this contact.", False
        if not is_business_email(context.contact_email) or not context.verified_business_email:
            return "Recipient is not a verified business-domain email.", False
        if context.no_contact_statement:
            return "The publication source contains a no-contact statement.", False
        if context.consent_type != "IMPLIED_CONSPICUOUS":
            return "Required conspicuous-publication consent evidence is missing.", False
        if not context.consent_source_url.startswith(("https://", "http://")):
            return "Consent evidence URL is missing or invalid.", False
        if not score.eligible or score.total < self.autonomy.minimum_total_score:
            return "Account does not clear the qualification threshold.", False
        if score.risk > self.autonomy.maximum_risk_score:
            return "Account risk score exceeds the sending threshold.", False
        if self.autonomy.approval_required(touch.step_position, touch.requires_approval) and touch.approved_at is None:
            return "Human approval is required for this touch.", True
        return "", False
