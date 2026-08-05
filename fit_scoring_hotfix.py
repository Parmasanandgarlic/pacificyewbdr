from __future__ import annotations

from dataclasses import replace
from typing import Any

import email_copy_intelligence as copy
import lead_intelligence as intelligence


_ORIGINAL_STRATEGY_FROM_PAYLOAD = copy._strategy_from_payload
_INSTALLED = False


def deterministic_strategy_from_payload(
    payload: dict[str, Any],
    dossier: str,
    expected_offer_route: str,
) -> copy.Strategy:
    """Replace the model's incompatible top-level score with evidence scoring.

    The strategy model scores opportunity dimensions on a 0-5 scale. The
    production gate previously compared its unrelated top-level `fit_score`
    directly to a 65/100 threshold, which made otherwise supported prospects
    impossible to qualify. The existing opportunity formula already converts
    the evidence dimensions to a bounded 0-100 score, so it is the authoritative
    fit score.

    This does not bypass any source, role, route, confidence, copy-quality,
    suppression, one-touch, or delivery control.
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
        and fit_score >= intelligence.QUALIFICATION_THRESHOLD
        and opportunity.confidence >= 3
        and opportunity.recipient_relevance >= 3
    )

    model_score = copy._clamp_int(payload.get("fit_score"))
    model_flag = bool(payload.get("qualified"))
    reason = (
        f"{strategy.reason} Deterministic evidence score={fit_score}; "
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
    global _INSTALLED
    if _INSTALLED:
        return
    copy._strategy_from_payload = deterministic_strategy_from_payload
    _INSTALLED = True
