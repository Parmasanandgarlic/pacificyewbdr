from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Callable, Iterator
from uuid import UUID

from .policies import normalize_email


class PostgresBase:
    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connection_factory = connection_factory

    @classmethod
    def from_database_url(cls, database_url: str) -> "PostgresRepository":
        if not database_url:
            raise ValueError("DATABASE_URL is required")
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError("Install psycopg[binary] to use PostgresRepository") from exc
        return cls(lambda: psycopg.connect(database_url))

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        connection = self._connection_factory()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, default=str, separators=(",", ":"))

    def bootstrap_runtime(
        self,
        *,
        mailbox_email: str,
        campaign_name: str,
        daily_limit: int = 8,
        enable_mailbox: bool = False,
    ) -> tuple[UUID, UUID]:
        if daily_limit < 0 or daily_limit > 500:
            raise ValueError("daily_limit must be between 0 and 500")
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into bdr_mailboxes(email,provider,enabled,daily_limit,health_status)
                values (%s,'zoho',%s,%s,'unknown')
                on conflict ((lower(email))) do update set
                    daily_limit=excluded.daily_limit,updated_at=now()
                returning id
                """,
                (normalize_email(mailbox_email), enable_mailbox, daily_limit),
            )
            mailbox_id = cur.fetchone()[0]
            cur.execute(
                """
                insert into bdr_campaigns(name,status,target_description)
                values (%s,'draft','Pacific Yew evidence-backed SMB outreach')
                returning id
                """,
                (campaign_name,),
            )
            campaign_id = cur.fetchone()[0]
            return mailbox_id, campaign_id

    def list_pending_approvals(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select t.id,a.name,c.email,a.total_score,a.risk_score,a.recommended_offer,
                       s.position,t.scheduled_for,t.subject,t.body
                from bdr_touches t
                join bdr_enrollments e on e.id=t.enrollment_id
                join bdr_accounts a on a.id=e.account_id
                join bdr_contacts c on c.id=e.contact_id
                join bdr_sequence_steps s on s.id=t.sequence_step_id
                where t.status='scheduled' and s.requires_approval and t.approved_at is null
                  and e.status='active'
                order by t.scheduled_for,s.position
                limit %s
                """,
                (max(1, min(limit, 200)),),
            )
            keys = (
                "touch_id",
                "account_name",
                "contact_email",
                "total_score",
                "risk_score",
                "recommended_offer",
                "step_position",
                "scheduled_for",
                "subject",
                "body",
            )
            return [dict(zip(keys, row)) for row in cur.fetchall()]

    def daily_briefing(self) -> dict[str, int]:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select
                  (select count(*) from bdr_accounts where created_at >= now() - interval '24 hours'),
                  (select count(*) from bdr_accounts where eligible),
                  (select count(*) from bdr_touches t join bdr_sequence_steps s on s.id=t.sequence_step_id
                     where t.status='scheduled' and s.requires_approval and t.approved_at is null),
                  (select count(*) from bdr_touches where status='scheduled' and scheduled_for <= now()),
                  (select count(*) from bdr_replies where received_at >= now() - interval '24 hours'),
                  (select count(*) from bdr_opportunities where stage in ('qualified','meeting_requested','meeting_booked','proposal')),
                  (select count(*) from bdr_suppressions where created_at >= now() - interval '24 hours')
                """
            )
            row = cur.fetchone()
            keys = (
                "accounts_researched_24h",
                "eligible_accounts",
                "pending_approvals",
                "due_touches",
                "replies_24h",
                "open_opportunities",
                "suppressions_24h",
            )
            return dict(zip(keys, row))

    def list_opportunities(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select o.id,a.name,c.email,o.offer,o.stage,o.summary,o.next_action,o.next_action_at,o.created_at
                from bdr_opportunities o
                join bdr_accounts a on a.id=o.account_id
                join bdr_contacts c on c.id=o.contact_id
                order by coalesce(o.next_action_at,o.created_at),o.created_at desc
                limit %s
                """,
                (max(1, min(limit, 500)),),
            )
            keys = (
                "opportunity_id",
                "account_name",
                "contact_email",
                "offer",
                "stage",
                "summary",
                "next_action",
                "next_action_at",
                "created_at",
            )
            return [dict(zip(keys, row)) for row in cur.fetchall()]
