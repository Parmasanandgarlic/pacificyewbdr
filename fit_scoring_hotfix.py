from __future__ import annotations

from dataclasses import replace
from typing import Any

import email_copy_intelligence as copy
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


def install() -> None:
    """Install one qualification boundary across the complete worker."""
    global _INSTALLED
    # growth_engine and streaming_growth import this shared module object, so
    # this aligns every downstream approval check before any Sheet row is read.
    intelligence.QUALIFICATION_THRESHOLD = MIN_EVIDENCE_SCORE
    if _INSTALLED:
        return
    copy._strategy_from_payload = deterministic_strategy_from_payload
    _INSTALLED = True
