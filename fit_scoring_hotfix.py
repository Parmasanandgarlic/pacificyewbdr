from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

import gspread

import email_copy_intelligence as copy
import growth_engine as growth
import lead_intelligence as intelligence


# The evidence-ranked opportunity engine defines 60 as its minimum supported
# opportunity. Production must use that same boundary everywhere: strategy,
# approval, queue, and delivery preparation.
MIN_EVIDENCE_SCORE = 60
_ORIGINAL_STRATEGY_FROM_PAYLOAD = copy._strategy_from_payload
_INSTALLED = False


def deterministic_strategy_from_payload(
    payload: dict[str, Any],
    dossier: str,
    expected_offer_route: str,
) -> copy.Strategy:
    """Use evidence-ranked opportunity scoring as the authoritative fit score.

    The model scores opportunity dimensions on a 0-5 scale. The opportunity
    engine converts those dimensions to a bounded 0-100 score. The model's
    separate top-level `fit_score` and `qualified` fields are advisory only.

    Exact-source, role, route, confidence, copy-quality, suppression, one-touch,
    ledger, delivery-window, and volume controls remain mandatory.
    """
    strategy = _ORIGINAL_STRATEGY_FROM_PAYLOAD(
        payload,
        dossier,
        expected_offer_route,
    )
    opportunity = strategy.opportunity
    fit_score = opportunity.score if opportunity is not None else 0
    returned_route = copy._clean(payload.get("offer_route"))

    qualified = bool(
        opportunity is not None
        and returned_route == expected_offer_route
        and strategy.recipient_role
        and strategy.role_relevance
        and fit_score >= MIN_EVIDENCE_SCORE
        and opportunity.confidence >= 3
        and opportunity.recipient_relevance >= 3
    )

    model_score = copy._clamp_int(payload.get("fit_score"))
    model_flag = bool(payload.get("qualified"))
    reason = (
        f"{strategy.reason} Deterministic evidence score={fit_score}; "
        f"minimum evidence score={MIN_EVIDENCE_SCORE}; "
        f"model advisory score={model_score}; "
        f"model advisory qualified={str(model_flag).lower()}."
    ).strip()

    return replace(
        strategy,
        qualified=qualified,
        fit_score=fit_score,
        reason=reason,
    )


def approve_evidence_ready_drafts() -> int:
    """Approve current drafts and safely recover threshold-stranded reviews.

    Yesterday's mismatched 65-point approval gate moved otherwise valid
    evidence-ranked rows to NEEDS_REVIEW. Reconsider only rows produced by the
    deterministic evidence pipeline, then apply every current legal,
    commercial, copy-quality, suppression, and sent-ledger gate again. No row
    is approved merely because it was previously reviewed.
    """
    approved = 0
    recovered = 0
    try:
        ws = growth.legacy.get_sheet()
        growth.legacy._sheets_throttle()
        values = ws.get_all_values()
        if len(values) < 2:
            return 0

        headers = values[0]
        required = {
            "email",
            "status",
            "source_url",
            "consent_type",
            "consent_observed_at",
            "consent_evidence_hash",
            "recipient_role",
            "role_relevance",
            "fit_score",
            "primary_signal",
            "research_evidence_url",
            "offer_route",
            "email_subject",
            "email_body",
        }
        if not required.issubset(headers):
            print(f"[growth] approval gate missing columns: {sorted(required - set(headers))}")
            return 0

        index = {name: headers.index(name) for name in required}
        analysis_index = headers.index("agent_analysis") if "agent_analysis" in headers else None
        updates: list[gspread.Cell] = []

        for row_number, row in enumerate(values[1:], start=2):
            def value(name: str) -> str:
                position = index[name]
                return (row[position] if len(row) > position else "").strip()

            status = value("status").upper()
            analysis = (
                (row[analysis_index] if analysis_index is not None and len(row) > analysis_index else "")
                .strip()
            )
            deterministic_review = (
                status == "NEEDS_REVIEW"
                and "Deterministic evidence score=" in analysis
            )
            if status != "DRAFT_READY" and not deterministic_review:
                continue

            email_address = value("email")
            try:
                score = int(value("fit_score"))
            except Exception:
                score = 0

            legal_ok = (
                growth.legacy.is_business_email(email_address)
                and not growth._is_placeholder_email(email_address)
                and value("consent_type").upper() == "IMPLIED_CONSPICUOUS"
                and value("source_url").startswith(("http://", "https://"))
                and bool(value("consent_observed_at"))
                and bool(re.fullmatch(r"[a-f0-9]{64}", value("consent_evidence_hash").lower()))
                and bool(value("recipient_role"))
                and bool(value("role_relevance"))
                and not growth.legacy.is_blocked(email_address)
                and not growth.legacy._in_sent_ledger(email_address)
            )
            commercial_ok = (
                score >= MIN_EVIDENCE_SCORE
                and bool(value("primary_signal"))
                and value("primary_signal").lower() != "none"
                and value("research_evidence_url").startswith(("http://", "https://"))
                and value("offer_route") in growth.ALLOWED_OFFER_ROUTES
            )
            quality_ok, _reason = intelligence.validate_draft(
                value("email_subject"),
                value("email_body"),
            )

            if legal_ok and commercial_ok and quality_ok:
                updates.append(
                    gspread.Cell(
                        row=row_number,
                        col=index["status"] + 1,
                        value="APPROVED",
                    )
                )
                approved += 1
                if deterministic_review:
                    recovered += 1
            elif status == "DRAFT_READY":
                updates.append(
                    gspread.Cell(
                        row=row_number,
                        col=index["status"] + 1,
                        value="NEEDS_REVIEW",
                    )
                )

        if updates:
            growth.legacy._sheets_throttle()
            ws.update_cells(updates)
            growth.legacy._CONTACTED_CACHE = None
    except Exception as exc:
        print(f"[growth] approval gate error: {exc}")
        return 0

    print(f"[growth] enhanced approvals={approved}; recovered_reviews={recovered}.")
    return approved


def install() -> None:
    """Install one qualification boundary across the complete worker."""
    global _INSTALLED
    # growth_engine and streaming_growth import this shared module object, so
    # this aligns every downstream approval check before any Sheet row is read.
    intelligence.QUALIFICATION_THRESHOLD = MIN_EVIDENCE_SCORE
    growth.approve_growth_drafts = approve_evidence_ready_drafts
    if _INSTALLED:
        return
    copy._strategy_from_payload = deterministic_strategy_from_payload
    _INSTALLED = True
