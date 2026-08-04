from __future__ import annotations

from datetime import datetime
from uuid import UUID

from .models import ClaimedTouch, DispatchContext, ProviderReceipt, Scorecard


class PostgresDeliveryMixin:
    def claim_due_touches(self, *, worker_id: str, limit: int, now: datetime) -> list[ClaimedTouch]:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("select * from claim_bdr_touches(%s,%s,%s)", (worker_id, limit, now))
            return [
                ClaimedTouch(
                    touch_id=row[0],
                    enrollment_id=row[1],
                    campaign_id=row[2],
                    account_id=row[3],
                    contact_id=row[4],
                    mailbox_id=row[5],
                    sequence_step_id=row[6],
                    step_position=row[7],
                    subject=row[8],
                    body=row[9],
                    requires_approval=row[10],
                    approved_at=row[11],
                    idempotency_key=row[12],
                )
                for row in cur.fetchall()
            ]

    def get_dispatch_context(self, touch_id: UUID) -> DispatchContext:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("select * from get_bdr_dispatch_context(%s)", (touch_id,))
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"touch not found: {touch_id}")
            claimed = ClaimedTouch(
                touch_id=row[0],
                enrollment_id=row[1],
                campaign_id=row[2],
                account_id=row[3],
                contact_id=row[4],
                mailbox_id=row[5],
                sequence_step_id=row[6],
                step_position=row[7],
                subject=row[8],
                body=row[9],
                requires_approval=row[10],
                approved_at=row[11],
                idempotency_key=row[12],
            )
            scorecard = Scorecard(
                fit=row[20],
                timing=row[21],
                authority=row[22],
                evidence=row[23],
                risk=row[24],
                total=row[25],
                eligible=row[26],
                reasons=tuple(row[27] or []),
            )
            return DispatchContext(
                touch=claimed,
                account_name=row[13],
                account_domain=row[14],
                contact_email=row[15],
                consent_type=row[16],
                consent_source_url=row[17],
                no_contact_statement=row[18],
                verified_business_email=row[19],
                scorecard=scorecard,
                suppressed=row[28],
                mailbox_enabled=row[29],
                mailbox_daily_limit=row[30],
                mailbox_sent_today=row[31],
                conflicting_active_enrollment=row[32],
            )

    def reserve_message(self, touch_id: UUID, idempotency_key: str) -> UUID | None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("select reserve_bdr_message(%s,%s)", (touch_id, idempotency_key))
            return cur.fetchone()[0]

    def mark_sent(self, touch_id: UUID, message_id: UUID, receipt: ProviderReceipt) -> None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                "select complete_bdr_message(%s,%s,%s,%s::jsonb)",
                (
                    message_id,
                    receipt.provider_message_id,
                    receipt.accepted_at,
                    self._json(dict(receipt.raw)),
                ),
            )

    def mark_failed(
        self,
        touch_id: UUID,
        message_id: UUID | None,
        reason: str,
        uncertain: bool,
    ) -> None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                "select fail_bdr_message(%s,%s,%s,%s)",
                (touch_id, message_id, reason, uncertain),
            )

    def release_touch(self, touch_id: UUID, reason: str) -> None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update bdr_touches set status='scheduled',claimed_at=null,claimed_by='',
                    last_error=%s,updated_at=now()
                where id=%s and status in ('claimed','reserved')
                """,
                (reason[:2000], touch_id),
            )
