from __future__ import annotations

from typing import Any, Mapping
from uuid import UUID

from .models import InboundReply, Opportunity, OutcomeEvent, ReplyAnalysis, RouteDecision
from .policies import normalize_email


class PostgresPipelineMixin:
    def pause_enrollment(self, enrollment_id: UUID, reason: str) -> None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                "update bdr_enrollments set status='paused',pause_reason=%s,updated_at=now() where id=%s",
                (reason, enrollment_id),
            )

    def stop_enrollment(self, enrollment_id: UUID, reason: str) -> None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("select stop_bdr_enrollment(%s,%s)", (enrollment_id, reason))

    def suppress(self, email: str, reason: str, source: str) -> None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into bdr_suppressions(email,reason,source)
                values (%s,%s,%s)
                on conflict ((lower(email))) do update set reason=excluded.reason,source=excluded.source,updated_at=now()
                """,
                (normalize_email(email), reason, source),
            )

    def record_reply(
        self,
        reply: InboundReply,
        analysis: ReplyAnalysis,
        contact_id: UUID,
        enrollment_id: UUID | None,
    ) -> tuple[UUID, bool]:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into bdr_replies
                    (contact_id,enrollment_id,sender_email,recipient_email,subject,body_text,
                     provider_message_id,received_at,intent,confidence,summary,action,headers)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                on conflict (provider_message_id) do nothing
                returning id
                """,
                (
                    contact_id, enrollment_id, reply.sender_email, reply.recipient_email,
                    reply.subject, reply.body_text, reply.provider_message_id, reply.received_at,
                    analysis.intent.value, analysis.confidence, analysis.summary,
                    analysis.action.value, self._json(dict(reply.headers)),
                ),
            )
            row = cur.fetchone()
            if row is not None:
                return row[0], True
            cur.execute(
                "select id from bdr_replies where provider_message_id=%s",
                (reply.provider_message_id,),
            )
            existing = cur.fetchone()
            if existing is None:
                raise RuntimeError("Reply idempotency conflict could not be resolved")
            return existing[0], False

    def create_opportunity(self, opportunity: Opportunity) -> UUID:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into bdr_opportunities
                    (account_id,contact_id,offer,stage,source_reply_id,summary,next_action,next_action_at)
                values (%s,%s,%s,%s,%s,%s,%s,%s)
                returning id
                """,
                (
                    opportunity.account_id,
                    opportunity.contact_id,
                    opportunity.offer.value,
                    opportunity.stage.value,
                    opportunity.source_reply_id,
                    opportunity.summary,
                    opportunity.next_action,
                    opportunity.next_action_at,
                ),
            )
            return cur.fetchone()[0]

    def get_contact_context_by_email(
        self, email: str
    ) -> tuple[UUID, UUID, UUID | None, RouteDecision | None] | None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select c.account_id,c.id,e.id,a.recommended_offer,a.route_rationale,a.route_confidence
                from bdr_contacts c
                join bdr_accounts a on a.id=c.account_id
                left join lateral (
                    select id from bdr_enrollments
                    where contact_id=c.id and status in ('active','paused')
                    order by created_at desc limit 1
                ) e on true
                where lower(c.email)=lower(%s)
                """,
                (normalize_email(email),),
            )
            row = cur.fetchone()
            if row is None:
                return None
            route = None
            if row[3]:
                from .models import Offer

                route = RouteDecision(Offer(row[3]), row[4] or "", float(row[5] or 0))
            return row[0], row[1], row[2], route

    def audit(
        self,
        event_type: str,
        entity_type: str,
        entity_id: UUID | None,
        payload: Mapping[str, Any],
    ) -> None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into bdr_audit_events(event_type,entity_type,entity_id,payload)
                values (%s,%s,%s,%s::jsonb)
                """,
                (event_type, entity_type, entity_id, self._json(dict(payload))),
            )

    def record_outcome(self, event: OutcomeEvent) -> None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into bdr_outcomes
                    (account_id,campaign_id,enrollment_id,outcome,occurred_at,value_cents,metadata)
                values (%s,%s,%s,%s,%s,%s,%s::jsonb)
                """,
                (
                    event.account_id,
                    event.campaign_id,
                    event.enrollment_id,
                    event.outcome,
                    event.occurred_at,
                    event.value_cents,
                    self._json(dict(event.metadata)),
                ),
            )
