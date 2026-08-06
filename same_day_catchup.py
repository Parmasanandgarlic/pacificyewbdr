from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import outreach_compliance as compliance

PACIFIC = ZoneInfo("America/Vancouver")
_INSTALLED = False
_ORIGINAL_DELIVERY_WINDOW = None


def _enabled() -> bool:
    return os.environ.get("ALLOW_SAME_DAY_CATCHUP", "false").strip().lower() == "true"


def _cutoff_minutes() -> int:
    raw = os.environ.get("SAME_DAY_CATCHUP_CUTOFF_MINUTES", "1020")
    try:
        return max(14 * 60, min(int(raw), 18 * 60))
    except (TypeError, ValueError):
        return 17 * 60


def same_day_delivery_window_ok(now: datetime | None = None) -> tuple[bool, str]:
    """Permit a missed scheduled slot to recover later the same business day.

    This does not create a fifth/manual send path. The slot must still be one of
    the four governed slots, Run Control still enforces the per-slot cap, and
    the mailbox daily cap remains authoritative. Catch-up closes at a bounded
    Pacific-time cutoff so outreach cannot drift into evenings.
    """
    if os.environ.get("INITIAL_OUTREACH_ONLY", "true").strip().lower() != "true":
        return False, "INITIAL_OUTREACH_ONLY must remain true"

    slot = os.environ.get("BDR_RUN_SLOT", "manual").strip().lower()
    if slot == "manual":
        return False, "manual delivery is disabled; only four scheduled runs may send"

    expected_minutes = compliance._ALLOWED_SLOTS.get(slot)
    if expected_minutes is None:
        return False, f"unapproved delivery slot: {slot}"

    local_now = (now or datetime.now(PACIFIC)).astimezone(PACIFIC)
    if local_now.weekday() >= 5:
        return False, "scheduled cold outreach is limited to weekdays"

    current_minutes = local_now.hour * 60 + local_now.minute
    if current_minutes < expected_minutes:
        return False, f"slot {slot} is not due yet"
    if current_minutes - expected_minutes <= 75:
        return True, "ok"
    if not _enabled():
        return False, f"outside approved business-hour window for slot {slot}"
    if current_minutes > _cutoff_minutes():
        return False, "same-day catch-up cutoff has passed"
    return True, "same-day catch-up"


def install() -> None:
    global _INSTALLED, _ORIGINAL_DELIVERY_WINDOW
    if _INSTALLED:
        return
    _ORIGINAL_DELIVERY_WINDOW = compliance._delivery_window_ok
    compliance._delivery_window_ok = same_day_delivery_window_ok
    _INSTALLED = True


# Production recovery trigger: a fresh main-branch push is required so GitHub
# evaluates the current workflow definition and catch-up environment variables.
