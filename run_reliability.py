from __future__ import annotations

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Callable

import bdr_agent as legacy
import outreach_compliance as compliance
import schedule_control

PACIFIC = ZoneInfo("America/Vancouver")
_INSTALLED = False
_PRIOR_PREFLIGHT: Callable[[], bool] | None = None
_PRIOR_APPEND: Callable[..., object] | None = None


def strict_sent_ledger_emails() -> set[str]:
    worksheet = legacy._ensure_ledger()
    legacy._sheets_throttle()
    rows = worksheet.get_all_values()
    if not rows:
        raise RuntimeError("Sent Ledger is unreadable")
    headers = rows[0]
    required = {"email", "sent_at"}
    if not required.issubset(headers):
        raise RuntimeError(f"Sent Ledger missing columns: {sorted(required - set(headers))}")
    email_index = headers.index("email")
    result = {
        row[email_index].strip().lower()
        for row in rows[1:]
        if len(row) > email_index and row[email_index].strip() and "@" in row[email_index]
    }
    legacy._LEDGER_CACHE = result
    return result


def strict_dnc_set() -> set[str]:
    worksheet = legacy.get_dnc_worksheet()
    legacy._sheets_throttle()
    rows = worksheet.get_all_values()
    if not rows:
        raise RuntimeError("Do Not Contact ledger is unreadable")
    headers = rows[0]
    if "email" not in headers:
        raise RuntimeError("Do Not Contact ledger is missing email column")
    email_index = headers.index("email")
    blocked = set(legacy.DNC_EMAILS)
    blocked.update(
        row[email_index].strip().lower()
        for row in rows[1:]
        if len(row) > email_index and row[email_index].strip()
    )
    legacy._BLOCKED_CACHE = blocked
    return blocked


def delivery_window_ok_at(slot: str, now: datetime) -> tuple[bool, str]:
    if os.environ.get("INITIAL_OUTREACH_ONLY", "true").strip().lower() != "true":
        return False, "INITIAL_OUTREACH_ONLY must remain true"
    if slot == "manual" and os.environ.get("ALLOW_MANUAL_DELIVERY", "false").strip().lower() != "true":
        return False, "manual delivery is disabled; only four scheduled runs may send"
    expected_minutes = schedule_control.SLOT_MINUTES.get(slot)
    if expected_minutes is None:
        return False, f"unapproved delivery slot: {slot}"
    local_now = now.astimezone(PACIFIC)
    if local_now.weekday() >= 5:
        return False, "scheduled cold outreach is limited to weekdays"
    current_minutes = local_now.hour * 60 + local_now.minute
    delay = current_minutes - expected_minutes
    if delay < 0 or delay > schedule_control.RECOVERY_WINDOW_MINUTES:
        return False, f"outside approved business-hour recovery window for slot {slot}"
    return True, "ok"


def reliable_delivery_window_ok() -> tuple[bool, str]:
    slot = os.environ.get("BDR_RUN_SLOT", "manual").strip().lower()
    return delivery_window_ok_at(slot, datetime.now(PACIFIC))


def _attempt(label: str, operation: Callable[[], bool], attempts: int = 3) -> bool:
    for attempt in range(1, attempts + 1):
        try:
            if operation():
                if attempt > 1:
                    print(f"[reliability] {label} recovered on attempt {attempt}.")
                return True
        except Exception as exc:
            print(f"[reliability] {label} attempt {attempt}/{attempts} failed: {exc}")
        if attempt < attempts:
            time.sleep(2 ** attempt)
    print(f"[reliability] {label} exhausted {attempts} attempts.")
    return False


def retrying_preflight() -> bool:
    assert _PRIOR_PREFLIGHT is not None

    def check() -> bool:
        legacy._LEDGER_CACHE = None
        legacy._BLOCKED_CACHE = None
        if not _PRIOR_PREFLIGHT():
            return False
        strict_sent_ledger_emails()
        strict_dnc_set()
        if compliance._load_one_touch_keys(force=True) is None:
            raise RuntimeError("One Touch Ledger is unreadable")
        return True

    return _attempt("production preflight", check)


def pacific_effective_send_limit(requested: int) -> int:
    local_today = datetime.now(PACIFIC).date()
    worksheet = legacy._ensure_ledger()
    legacy._sheets_throttle()
    rows = worksheet.get_all_values()
    if not rows:
        raise RuntimeError("Sent Ledger could not be read for the Pacific-day cap")
    headers = rows[0]
    required = {"email", "sent_at"}
    if not required.issubset(headers):
        raise RuntimeError(f"Sent Ledger missing columns: {sorted(required - set(headers))}")
    email_index = headers.index("email")
    sent_index = headers.index("sent_at")
    sent_today = 0
    for row in rows[1:]:
        if len(row) <= max(email_index, sent_index) or not row[email_index].strip():
            continue
        try:
            sent_at = datetime.fromisoformat(row[sent_index].replace("Z", "+00:00"))
            if sent_at.astimezone(PACIFIC).date() == local_today:
                sent_today += 1
        except Exception:
            continue
    daily_cap = max(1, int(os.environ.get("DAILY_SEND_CAP", "32")))
    effective = min(max(0, requested), max(0, daily_cap - sent_today))
    print(
        f"[delivery-health] requested={requested} effective={effective} "
        f"Pacific-day sent={sent_today}/{daily_cap}"
    )
    return effective


def strict_append_to_ledgers(
    email_address: str,
    business_name: str,
    subject: str,
    source: str = "bdr_agent",
):
    assert _PRIOR_APPEND is not None
    result = _PRIOR_APPEND(email_address, business_name, subject, source)
    legacy._LEDGER_CACHE = None
    if email_address.strip().lower() not in strict_sent_ledger_emails():
        raise RuntimeError(f"Sent Ledger did not persist {email_address}")
    keys = compliance._load_one_touch_keys(force=True)
    if keys is None or email_address.strip().lower() not in keys["emails"]:
        raise RuntimeError(f"One Touch Ledger did not persist {email_address}")
    return result


def install() -> None:
    global _INSTALLED, _PRIOR_PREFLIGHT, _PRIOR_APPEND
    if _INSTALLED:
        return
    _PRIOR_PREFLIGHT = legacy.preflight_checks
    _PRIOR_APPEND = legacy._append_to_ledger
    compliance._delivery_window_ok = reliable_delivery_window_ok
    legacy.preflight_checks = retrying_preflight
    legacy._load_ledger_emails = strict_sent_ledger_emails
    legacy._blocked_set = strict_dnc_set
    legacy._append_to_ledger = strict_append_to_ledgers
    _INSTALLED = True
