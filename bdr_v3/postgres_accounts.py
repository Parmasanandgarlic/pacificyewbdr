from __future__ import annotations

from datetime import datetime
from typing import Sequence
from uuid import UUID

from .models import Contact, Evidence, RouteDecision, Scorecard, SequencePlan, VerifiedAccount
from .policies import make_idempotency_key, next_business_send_time, normalize_email


class PostgresAccountMixin:
    def upsert_account(self, account: VerifiedAccount) -> UUID:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into bdr_accounts
                    (name, website, domain, source_url, location, is_operating_business,
                     verification_confidence, verification_reason, metadata)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                on conflict (domain) do update set
                    name=excluded.name,
                    website=excluded.website,
                    source_url=excluded.source_url,
                    location=excluded.location,
                    is_operating_business=excluded.is_operating_business,
                    verification_confidence=excluded.verification_confidence,
                    verification_reason=excluded.verification_reason,
                    metadata=excluded.metadata,
                    updated_at=now()
                returning id
                """,
                (
                    account.name,
                    account.website,
                    account.domain,
                    account.source_url,
                    account.location,
                    account.is_operating_business,
                    account.confidence,
                    account.reason,
                    self._json(dict(account.metadata)),
                ),
            )
            return cur.fetchone()[0]

    def upsert_contact(self, account_id: UUID, contact: Contact) -> UUID:
        email = normalize_email(contact.email)
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into bdr_contacts
                    (account_id,email,name,role,consent_type,consent_source_url,
                     verified_business_email,no_contact_statement,confidence)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                on conflict ((lower(email))) do update set
                    account_id=excluded.account_id,
                    name=excluded.name,
                    role=excluded.role,
                    consent_type=excluded.consent_type,
                    consent_source_url=excluded.consent_source_url,
                    verified_business_email=excluded.verified_business_email,
                    no_contact_statement=excluded.no_contact_statement,
                    confidence=excluded.confidence,
                    updated_at=now()
                returning id
                """,
                (
                    account_id,
                    email,
                    contact.name,
                    contact.role,
                    contact.consent_type,
                    contact.source_url,
                    contact.verified_business_email,
                    contact.no_contact_statement,
                    contact.confidence,
                ),
            )
            return cur.fetchone()[0]

    def replace_evidence(self, account_id: UUID, evidence: Sequence[Evidence]) -> None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("delete from bdr_evidence where account_id=%s", (account_id,))
            cur.executemany(
                """
                insert into bdr_evidence
                    (account_id,kind,claim,source_url,excerpt,confidence,observed_at)
                values (%s,%s,%s,%s,%s,%s,%s)
                """,
                [
                    (
                        account_id,
                        item.kind.value,
                        item.claim,
                        item.source_url,
                        item.excerpt,
                        item.confidence,
                        item.observed_at,
                    )
                    for item in evidence
                ],
            )

    def save_scorecard(self, account_id: UUID, scorecard: Scorecard) -> None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update bdr_accounts set
                    fit_score=%s,timing_score=%s,authority_score=%s,evidence_score=%s,
                    risk_score=%s,total_score=%s,eligible=%s,score_reasons=%s::jsonb,updated_at=now()
                where id=%s
                """,
                (
                    scorecard.fit,
                    scorecard.timing,
                    scorecard.authority,
                    scorecard.evidence,
                    scorecard.risk,
                    scorecard.total,
                    scorecard.eligible,
                    self._json(list(scorecard.reasons)),
                    account_id,
                ),
            )

    def save_route(self, account_id: UUID, route: RouteDecision) -> None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update bdr_accounts set recommended_offer=%s,route_rationale=%s,
                    route_confidence=%s,updated_at=now() where id=%s
                """,
                (route.offer.value, route.rationale, route.confidence, account_id),
            )

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
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into bdr_enrollments
                    (campaign_id,account_id,contact_id,mailbox_id,status,offer,timezone)
                values (%s,%s,%s,%s,'active',%s,%s)
                on conflict (campaign_id,contact_id) where status in ('active','paused')
                do update set updated_at=now()
                returning id
                """,
                (campaign_id, account_id, contact_id, mailbox_id, plan.offer.value, plan.timezone),
            )
            enrollment_id = cur.fetchone()[0]
            for step in plan.steps:
                cur.execute(
                    """
                    insert into bdr_sequence_steps
                        (campaign_id,position,delay_days,purpose,subject_template,body_template,requires_approval)
                    values (%s,%s,%s,%s,%s,%s,%s)
                    on conflict (campaign_id,position) do update set
                        delay_days=excluded.delay_days,purpose=excluded.purpose,
                        subject_template=excluded.subject_template,body_template=excluded.body_template,
                        requires_approval=excluded.requires_approval
                    returning id
                    """,
                    (
                        campaign_id,
                        step.position,
                        step.delay_days,
                        step.purpose,
                        step.subject_template,
                        step.body_template,
                        step.requires_approval,
                    ),
                )
                step_id = cur.fetchone()[0]
                cur.execute(
                    """
                    insert into bdr_touches
                        (enrollment_id,sequence_step_id,scheduled_for,status,idempotency_key,subject,body)
                    values (%s,%s,%s,'scheduled',%s,%s,%s)
                    on conflict (idempotency_key) do nothing
                    """,
                    (
                        enrollment_id,
                        step_id,
                        next_business_send_time(start_at, step.delay_days, plan.timezone),
                        make_idempotency_key(campaign_id, contact_id, step.position),
                        step.subject_template,
                        step.body_template,
                    ),
                )
            return enrollment_id

    def approve_touch(self, touch_id: UUID, approved_by: str) -> None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update bdr_touches set approved_at=now(),approved_by=%s,updated_at=now()
                where id=%s and status='scheduled'
                """,
                (approved_by, touch_id),
            )
            if cur.rowcount != 1:
                raise ValueError("Touch is not in an approvable scheduled state")
