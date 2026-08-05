from __future__ import annotations

from typing import Any, Callable

import bdr_agent as legacy
import outreach_compliance as compliance
import streaming_growth

_INSTALLED = False
_ORIGINAL_ENSURE_ONE_TOUCH: Callable[..., Any] | None = None
_ORIGINAL_ENSURE_SENT_LEDGER: Callable[..., Any] | None = None
_ORIGINAL_DNC_WORKSHEET: Callable[..., Any] | None = None
_ORIGINAL_FIND_LEAD: Callable[..., dict[str, Any]] | None = None
_ORIGINAL_APPEND_STREAM_ROWS: Callable[..., int] | None = None

_ONE_TOUCH_WORKSHEET = None
_SENT_LEDGER_WORKSHEET = None
_DNC_WORKSHEET = None
_LEADS_BY_EMAIL: dict[str, dict[str, Any]] | None = None


def _cached_one_touch_worksheet():
    global _ONE_TOUCH_WORKSHEET
    assert _ORIGINAL_ENSURE_ONE_TOUCH is not None
    if _ONE_TOUCH_WORKSHEET is None:
        _ONE_TOUCH_WORKSHEET = _ORIGINAL_ENSURE_ONE_TOUCH()
    return _ONE_TOUCH_WORKSHEET


def _cached_sent_ledger_worksheet():
    global _SENT_LEDGER_WORKSHEET
    assert _ORIGINAL_ENSURE_SENT_LEDGER is not None
    if _SENT_LEDGER_WORKSHEET is None:
        _SENT_LEDGER_WORKSHEET = _ORIGINAL_ENSURE_SENT_LEDGER()
    return _SENT_LEDGER_WORKSHEET


def _cached_dnc_worksheet():
    global _DNC_WORKSHEET
    assert _ORIGINAL_DNC_WORKSHEET is not None
    if _DNC_WORKSHEET is None:
        _DNC_WORKSHEET = _ORIGINAL_DNC_WORKSHEET()
    return _DNC_WORKSHEET


def _lead_cache() -> dict[str, dict[str, Any]]:
    global _LEADS_BY_EMAIL
    if _LEADS_BY_EMAIL is None:
        _LEADS_BY_EMAIL = {
            (lead.get("email") or "").strip().lower(): lead
            for lead in legacy.get_leads(limit=10000)
            if (lead.get("email") or "").strip()
        }
    return _LEADS_BY_EMAIL


def _cached_find_lead(email_address: str) -> dict[str, Any]:
    normalized = (email_address or "").strip().lower()
    lead = _lead_cache().get(normalized)
    if lead is not None:
        return lead
    assert _ORIGINAL_FIND_LEAD is not None
    lead = _ORIGINAL_FIND_LEAD(normalized)
    if lead:
        _lead_cache()[normalized] = lead
    return lead


def _append_stream_rows(rows: list[dict[str, Any]]) -> int:
    assert _ORIGINAL_APPEND_STREAM_ROWS is not None
    appended = _ORIGINAL_APPEND_STREAM_ROWS(rows)
    if appended and _LEADS_BY_EMAIL is not None:
        for row in rows:
            address = (row.get("email") or "").strip().lower()
            if address:
                _LEADS_BY_EMAIL[address] = dict(row)
    return appended


def _append_one_touch_without_immediate_reread(
    email_address: str,
    business_name: str,
    subject: str,
    source: str,
) -> None:
    """Append once, then let the strict reliability layer verify once.

    The previous implementation force-read the complete One Touch Ledger inside
    this function and the strict dual-ledger wrapper force-read it again
    immediately afterwards. That doubled the most expensive read path for every
    SMTP delivery and was a direct contributor to per-user Sheets 429 failures.
    """
    lead = _cached_find_lead(email_address)
    website = lead.get("website") or ""
    source_url = lead.get("source_url") or ""
    evidence_hash = lead.get("consent_evidence_hash") or ""
    run_id = lead.get("run_id") or ""
    normalized_email = email_address.strip().lower()
    domain = compliance._host(website) or compliance._email_domain(normalized_email)

    existing, _reason = compliance._one_touch_match(
        {
            "email": normalized_email,
            "website": website,
            "business_name": business_name,
        }
    )
    if existing and compliance._one_touch_cache is not None:
        return

    try:
        worksheet = _cached_one_touch_worksheet()
        worksheet.append_row(
            [
                normalized_email,
                (business_name or "").strip(),
                website,
                domain,
                compliance._utc_now(),
                (subject or "").strip(),
                source_url,
                evidence_hash,
                run_id or source,
            ]
        )
        # Keep the validated in-run suppression cache coherent without issuing
        # another read. run_reliability.strict_append_to_ledgers performs the
        # single authoritative force-read verification after both appends.
        keys = compliance._one_touch_cache
        if keys is not None:
            keys["emails"].add(normalized_email)
            normalized_site = compliance._normalized_site(website)
            normalized_name = compliance._normalized_name(business_name)
            if normalized_site:
                keys["websites"].add(normalized_site)
            if domain:
                keys["domains"].add(domain)
            if normalized_name:
                keys["names"].add(normalized_name)
    except Exception as exc:
        # Preserve the existing uncertainty-safe behavior: allow the Sent Ledger
        # append to run, then the strict verifier fails closed and retries the
        # missing One Touch record rather than silently declaring success.
        print(f"[one-touch] ERROR recording business-level send: {exc}")


def install() -> None:
    global _INSTALLED
    global _ORIGINAL_ENSURE_ONE_TOUCH, _ORIGINAL_ENSURE_SENT_LEDGER
    global _ORIGINAL_DNC_WORKSHEET, _ORIGINAL_FIND_LEAD, _ORIGINAL_APPEND_STREAM_ROWS
    if _INSTALLED:
        return

    _ORIGINAL_ENSURE_ONE_TOUCH = compliance._ensure_one_touch_ledger
    _ORIGINAL_ENSURE_SENT_LEDGER = legacy._ensure_ledger
    _ORIGINAL_DNC_WORKSHEET = legacy.get_dnc_worksheet
    _ORIGINAL_FIND_LEAD = compliance._find_lead
    _ORIGINAL_APPEND_STREAM_ROWS = streaming_growth.append_leads_safely

    compliance._ensure_one_touch_ledger = _cached_one_touch_worksheet
    legacy._ensure_ledger = _cached_sent_ledger_worksheet
    legacy.get_dnc_worksheet = _cached_dnc_worksheet
    compliance._find_lead = _cached_find_lead
    compliance._append_one_touch = _append_one_touch_without_immediate_reread
    streaming_growth.append_leads_safely = _append_stream_rows
    _INSTALLED = True
