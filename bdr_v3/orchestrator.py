from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Protocol
from uuid import UUID

from .delivery import GuardedDeliveryService
from .models import (
    AccountCandidate,
    AccountProcessResult,
    InboundReply,
    OutcomeEvent,
    ResearchPacket,
    VerifiedAccount,
)
from .policies import default_sequence, render_sequence, route_offer, score_research
from .replies import ReplyProcessResult, ReplyProcessor
from .repository import BdrRepository


class DiscoveryProvider(Protocol):
    def discover(self, query: str) -> Iterable[AccountCandidate]: ...


class AccountVerifier(Protocol):
    def verify(self, candidate: AccountCandidate) -> VerifiedAccount: ...


class ResearchProvider(Protocol):
    def research(self, account: VerifiedAccount) -> ResearchPacket: ...


@dataclass(frozen=True, slots=True)
class EmployeeConfig:
    mailbox_id: UUID
    campaign_id: UUID
    timezone: str = "America/Vancouver"
    maximum_candidates_per_run: int = 100
    dispatch_batch_size: int = 8


class BusinessDevelopmentEmployee:
    """Coordinate the complete business-development loop through isolated policies."""

    def __init__(
        self,
        *,
        repository: BdrRepository,
        discovery: DiscoveryProvider,
        verifier: AccountVerifier,
        researcher: ResearchProvider,
        delivery: GuardedDeliveryService,
        replies: ReplyProcessor,
        config: EmployeeConfig,
    ) -> None:
        self.repository = repository
        self.discovery = discovery
        self.verifier = verifier
        self.researcher = researcher
        self.delivery = delivery
        self.replies = replies
        self.config = config

    def process_candidate(self, candidate: AccountCandidate, *, start_at: datetime | None = None) -> AccountProcessResult:
        verified = self.verifier.verify(candidate)
        account_id = self.repository.upsert_account(verified)
        packet = self.researcher.research(verified)
        if packet.account.domain != verified.domain:
            raise ValueError("Research provider returned a different account domain")

        self.repository.replace_evidence(account_id, packet.evidence)
        scorecard = score_research(packet)
        route = route_offer(packet)
        self.repository.save_scorecard(account_id, scorecard)
        self.repository.save_route(account_id, route)

        contact_id = None
        enrollment_id = None
        status = "review"
        if packet.contact is not None:
            contact_id = self.repository.upsert_contact(account_id, packet.contact)

        if scorecard.eligible and contact_id is not None:
            plan = default_sequence(route.offer, timezone_name=self.config.timezone)
            rendered = render_sequence(
                plan,
                account_name=verified.name,
                contact_name=packet.contact.name if packet.contact else "",
                supported_observation=self._supported_observation(packet),
                business_problem=packet.business_problem,
            )
            enrollment_id = self.repository.create_enrollment(
                account_id=account_id,
                contact_id=contact_id,
                mailbox_id=self.config.mailbox_id,
                campaign_id=self.config.campaign_id,
                plan=rendered,
                start_at=start_at or datetime.now(timezone.utc),
            )
            status = "enrolled"
        elif packet.contact is None:
            status = "needs_contact"
        elif not scorecard.eligible:
            status = "nurture_or_review"

        self.repository.audit(
            "account_processed",
            "account",
            account_id,
            {
                "candidate_source": candidate.source_url,
                "status": status,
                "score_total": scorecard.total,
                "risk": scorecard.risk,
                "offer": route.offer.value,
                "contact_id": str(contact_id) if contact_id else None,
                "enrollment_id": str(enrollment_id) if enrollment_id else None,
            },
        )
        return AccountProcessResult(account_id, contact_id, scorecard, route, enrollment_id, status)

    def discover_and_prepare(self, query: str) -> list[AccountProcessResult]:
        results: list[AccountProcessResult] = []
        for index, candidate in enumerate(self.discovery.discover(query)):
            if index >= self.config.maximum_candidates_per_run:
                break
            try:
                results.append(self.process_candidate(candidate))
            except Exception as exc:
                self.repository.audit(
                    "candidate_processing_failed",
                    "candidate",
                    None,
                    {
                        "website": candidate.website,
                        "name": candidate.name,
                        "source_url": candidate.source_url,
                        "error": f"{exc.__class__.__name__}: {exc}",
                    },
                )
        return results

    def dispatch_due(self, *, worker_id: str, now: datetime | None = None):
        return self.delivery.dispatch_due(
            worker_id=worker_id,
            limit=self.config.dispatch_batch_size,
            now=now,
        )

    def process_reply(self, reply: InboundReply) -> ReplyProcessResult:
        return self.replies.process(reply)

    def learn_from_outcome(self, event: OutcomeEvent) -> None:
        self.repository.record_outcome(event)

    @staticmethod
    def _supported_observation(packet: ResearchPacket) -> str:
        workflow_evidence = next(
            (item for item in packet.evidence if item.kind.value in ("workflow", "service", "software")),
            None,
        )
        if workflow_evidence:
            return workflow_evidence.claim.rstrip(".")
        return "your public site shows a customer-facing operating workflow"
