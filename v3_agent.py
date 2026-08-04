from __future__ import annotations

import argparse
import os
import sys
from uuid import UUID

from bdr_v3.adapters import (
    HttpAccountVerifier,
    LegacyDiscoveryAdapter,
    LegacyResearchAdapter,
    LegacyZohoMailSender,
    OpenRouterStructuredResearchAdapter,
    ZohoMailboxReader,
    ZohoReplyResponder,
)
from bdr_v3.delivery import AutonomyPolicy, GuardedDeliveryService
from bdr_v3.orchestrator import BusinessDevelopmentEmployee, EmployeeConfig
from bdr_v3.postgres_repository import PostgresRepository
from bdr_v3.replies import ReplyPolicy, ReplyProcessor


def _required_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _v3_enabled() -> bool:
    return (os.environ.get("BDR_V3_ENABLED") or "").lower() in {"1", "true", "yes"}


def build_employee() -> tuple[BusinessDevelopmentEmployee, ZohoMailboxReader]:
    if not _v3_enabled():
        raise RuntimeError("BDR_V3_ENABLED must be true before the v3 worker can run")

    database_url = _required_env("DATABASE_URL")
    mailbox_id = UUID(_required_env("BDR_V3_MAILBOX_ID"))
    campaign_id = UUID(_required_env("BDR_V3_CAMPAIGN_ID"))
    zoho_user = _required_env("GMAIL_USER")
    zoho_password = _required_env("GMAIL_APP_PASSWORD")
    repository = PostgresRepository.from_database_url(database_url)

    openrouter_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    researcher = (
        OpenRouterStructuredResearchAdapter(
            api_key=openrouter_key,
            model=(os.environ.get("OPENROUTER_MODEL") or "openrouter/free").strip(),
        )
        if openrouter_key
        else LegacyResearchAdapter()
    )
    delivery = GuardedDeliveryService(
        repository,
        LegacyZohoMailSender(),
        autonomy=AutonomyPolicy(
            require_approval_for_initial=True,
            require_approval_for_followups=True,
            minimum_total_score=int(os.environ.get("BDR_V3_MINIMUM_SCORE", "55")),
            maximum_risk_score=int(os.environ.get("BDR_V3_MAXIMUM_RISK", "35")),
        ),
    )
    responder = ZohoReplyResponder(
        username=zoho_user,
        app_password=zoho_password,
        sender_name=os.environ.get("SENDER_NAME", "Pacific Yew Automations"),
    )
    replies = ReplyProcessor(
        repository,
        policy=ReplyPolicy(
            booking_url=(os.environ.get("BDR_BOOKING_URL") or "").strip(),
            auto_confirm_unsubscribe=(
                os.environ.get("BDR_AUTO_CONFIRM_UNSUBSCRIBE") or ""
            ).lower()
            == "true",
            auto_send_booking_link=(
                os.environ.get("BDR_AUTO_SEND_BOOKING_LINK") or ""
            ).lower()
            == "true",
        ),
        responder=responder,
    )
    employee = BusinessDevelopmentEmployee(
        repository=repository,
        discovery=LegacyDiscoveryAdapter(),
        verifier=HttpAccountVerifier(),
        researcher=researcher,
        delivery=delivery,
        replies=replies,
        config=EmployeeConfig(
            mailbox_id=mailbox_id,
            campaign_id=campaign_id,
            timezone=os.environ.get("BDR_TIMEZONE", "America/Vancouver"),
            maximum_candidates_per_run=int(
                os.environ.get("BDR_V3_MAX_CANDIDATES", "100")
            ),
            dispatch_batch_size=int(
                os.environ.get("BDR_V3_DISPATCH_BATCH", "8")
            ),
        ),
    )
    mailbox = ZohoMailboxReader(username=zoho_user, app_password=zoho_password)
    return employee, mailbox


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pacific Yew BDR v3 governed employee"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover_parser = subparsers.add_parser(
        "discover",
        help="Discover, verify, research, score, route, and enroll accounts",
    )
    discover_parser.add_argument("query")

    dispatch_parser = subparsers.add_parser(
        "dispatch",
        help="Claim and dispatch approved due touches",
    )
    dispatch_parser.add_argument("--worker-id", default=f"manual-{os.getpid()}")

    replies_parser = subparsers.add_parser(
        "replies",
        help="Process unread mailbox replies",
    )
    replies_parser.add_argument("--limit", type=int, default=50)

    pending_parser = subparsers.add_parser(
        "pending-approvals",
        help="List touches waiting for human approval",
    )
    pending_parser.add_argument("--limit", type=int, default=50)

    approve_parser = subparsers.add_parser(
        "approve-touch",
        help="Approve one scheduled touch after human review",
    )
    approve_parser.add_argument("touch_id")
    approve_parser.add_argument(
        "--approved-by",
        default=os.environ.get("USER", "human"),
    )

    bootstrap_parser = subparsers.add_parser(
        "bootstrap",
        help="Create the initial mailbox and draft campaign records",
    )
    bootstrap_parser.add_argument(
        "--mailbox-email",
        default=os.environ.get("GMAIL_USER", "contact@pacificyew.pro"),
    )
    bootstrap_parser.add_argument(
        "--campaign-name",
        default="Pacific Yew Local SMB Outreach",
    )
    bootstrap_parser.add_argument("--daily-limit", type=int, default=8)
    bootstrap_parser.add_argument("--enable-mailbox", action="store_true")

    all_parser = subparsers.add_parser(
        "all",
        help="Run discovery, replies, then approved dispatch",
    )
    all_parser.add_argument("query")
    all_parser.add_argument("--worker-id", default=f"manual-{os.getpid()}")

    args = parser.parse_args(argv)
    if args.command == "bootstrap":
        if not _v3_enabled():
            raise RuntimeError("BDR_V3_ENABLED must be true before bootstrap")
        repository = PostgresRepository.from_database_url(
            _required_env("DATABASE_URL")
        )
        mailbox_id, campaign_id = repository.bootstrap_runtime(
            mailbox_email=args.mailbox_email,
            campaign_name=args.campaign_name,
            daily_limit=args.daily_limit,
            enable_mailbox=args.enable_mailbox,
        )
        print(f"BDR_V3_MAILBOX_ID={mailbox_id}")
        print(f"BDR_V3_CAMPAIGN_ID={campaign_id}")
        return 0

    employee, mailbox = build_employee()

    if args.command == "discover":
        results = employee.discover_and_prepare(args.query)
        for result in results:
            print(
                f"{result.status}: account={result.account_id} "
                f"contact={result.contact_id} score={result.scorecard.total} "
                f"risk={result.scorecard.risk} offer={result.route.offer.value} "
                f"enrollment={result.enrollment_id}"
            )
        return 0

    if args.command == "dispatch":
        results = employee.dispatch_due(worker_id=args.worker_id)
        for result in results:
            print(
                f"{result.status.value}: touch={result.touch_id} "
                f"reason={result.reason}"
            )
        return 0 if all(
            result.status.value in {"sent", "cancelled"}
            for result in results
        ) else 2

    if args.command == "replies":
        for reply in mailbox.unread(limit=args.limit):
            result = employee.process_reply(reply)
            print(
                f"reply={reply.provider_message_id} "
                f"intent={result.analysis.intent.value} "
                f"action={result.analysis.action.value} "
                f"opportunity={result.opportunity_id}"
            )
        return 0

    if args.command == "pending-approvals":
        for item in employee.repository.list_pending_approvals(args.limit):
            print(
                f"touch={item['touch_id']} account={item['account_name']} "
                f"contact={item['contact_email']} score={item['total_score']} "
                f"risk={item['risk_score']} offer={item['recommended_offer']} "
                f"step={item['step_position']} scheduled={item['scheduled_for']} "
                f"subject={item['subject']}"
            )
        return 0

    if args.command == "approve-touch":
        employee.repository.approve_touch(UUID(args.touch_id), args.approved_by)
        print(f"approved touch {args.touch_id}")
        return 0

    if args.command == "all":
        employee.discover_and_prepare(args.query)
        for reply in mailbox.unread(limit=50):
            employee.process_reply(reply)
        employee.dispatch_due(worker_id=args.worker_id)
        return 0

    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            f"BDR v3 failed: {exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )
        raise
