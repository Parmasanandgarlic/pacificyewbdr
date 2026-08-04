from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import gspread
import requests

import bdr_agent as legacy
import growth_engine as growth
import lead_intelligence as intelligence
import streaming_growth

COMPLIANCE_COLUMNS = [
    "publication_method",
    "publication_page_host",
    "publication_first_party",
    "publication_role_rationale",
    "no_solicitation_checked_at",
    "no_solicitation_statement",
    "initial_outreach_only",
    "commercial_ad_disclosure",
    "compliance_profile",
]

COMPLIANCE_PROFILE = "CASL_CAN_SPAM_CONSERVATIVE_V1"
ONE_TOUCH_LEDGER_TAB = os.environ.get("ONE_TOUCH_LEDGER_TAB", "One Touch Ledger")
ONE_TOUCH_HEADERS = [
    "email",
    "business_name",
    "website",
    "business_domain",
    "sent_at",
    "subject",
    "source_url",
    "consent_evidence_hash",
    "run_id",
]

FIRST_PARTY_PATHS = (
    "",
    "/contact",
    "/contact-us",
    "/about",
    "/about-us",
    "/team",
    "/staff",
    "/locations",
)

COMMERCIAL_ROLE_PREFIXES = {
    "admin",
    "appointments",
    "bookings",
    "clinic",
    "contact",
    "estimates",
    "hello",
    "info",
    "manager",
    "office",
    "operations",
    "ops",
    "owner",
    "quotes",
    "reception",
    "sales",
    "service",
    "support",
}

BLOCKED_ROLE_PREFIXES = {
    "abuse",
    "careers",
    "compliance",
    "dmca",
    "hr",
    "jobs",
    "legal",
    "media",
    "noreply",
    "no-reply",
    "postmaster",
    "press",
    "privacy",
    "security",
    "webmaster",
}

ROLE_CONTEXT_PATTERN = re.compile(
    r"\b(owner|founder|manager|director|operations?|office|administration|"
    r"reception|appointments?|bookings?|sales|service|support|quotes?|estimates?)\b",
    re.I,
)

_ALLOWED_SLOTS = {
    "morning": 8 * 60,
    "late_morning": 10 * 60,
    "midday": 12 * 60,
    "afternoon": 14 * 60,
}

_installed = False
_prior_preflight: Callable[[], bool] | None = None
_prior_pre_send: Callable[[dict], tuple[bool, str]] | None = None
_prior_append_ledger: Callable[..., Any] | None = None
_prior_approve: Callable[[], int] | None = None
_prior_lead_record: Callable[..., dict[str, Any]] | None = None
_one_touch_cache: dict[str, set[str]] | None = None
_one_touch_error: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _host(url: str) -> str:
    host = urlparse(intelligence.normalize_url(url)).netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _same_site(left: str, right: str) -> bool:
    left_host = _host(left)
    right_host = _host(right)
    return bool(left_host and right_host) and left_host == right_host


def _email_domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower().strip(".") if "@" in address else ""


def _same_business_domain(site_url: str, address: str) -> bool:
    site_host = _host(site_url)
    email_domain = _email_domain(address)
    return bool(site_host and email_domain) and (
        site_host == email_domain
        or site_host.endswith("." + email_domain)
        or email_domain.endswith("." + site_host)
    )


def _normalized_site(url: str) -> str:
    return legacy._norm_url(url or "")


def _normalized_name(name: str) -> str:
    return legacy._normalize_name(name or "")


def _publication_candidates(page: intelligence.PageEvidence) -> list[tuple[int, str, str, str]]:
    """Return direct visible publications only; metadata and source-code-only values are excluded."""
    candidates: dict[str, tuple[int, str, str, str]] = {}
    for href, label in page.links:
        if not href.lower().startswith("mailto:"):
            continue
        raw = href.split(":", 1)[1].split("?", 1)[0]
        for address in legacy.EMAIL_RE.findall(raw):
            normalized = address.lower().strip(".")
            candidates[normalized] = (120, normalized, "MAILTO", label or normalized)

    for address in legacy.EMAIL_RE.findall(page.text or ""):
        normalized = address.lower().strip(".")
        candidates.setdefault(normalized, (80, normalized, "VISIBLE_TEXT", normalized))
    return list(candidates.values())


def _role_rationale(page: intelligence.PageEvidence, address: str, label: str) -> str:
    local = address.split("@", 1)[0].lower()
    if local in BLOCKED_ROLE_PREFIXES:
        return ""
    if local in COMMERCIAL_ROLE_PREFIXES:
        return f"Published {local} inbox is associated with commercial or operational business functions."

    excerpt = growth._email_excerpt(page, address)
    context = " ".join([label or "", excerpt or "", " ".join(page.headings[:8])])
    match = ROLE_CONTEXT_PATTERN.search(context)
    if match:
        return f"The publication context associates this address with {match.group(1).lower()} functions."
    return ""


def _evidence_hash(
    address: str,
    source_url: str,
    observed_at: str,
    excerpt: str,
    publication_method: str,
    role_rationale: str,
) -> str:
    payload = "\n".join(
        [address.lower(), source_url, observed_at, excerpt, publication_method, role_rationale]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def strict_first_party_email(website_url: str) -> growth.EmailEvidence:
    """Collect only directly published, first-party, role-relevant business addresses."""
    home = intelligence.normalize_url(website_url)
    if not home:
        return growth.EmailEvidence(reason="invalid website URL")

    seen: set[str] = set()
    candidates: list[tuple[int, str, str, intelligence.PageEvidence, str, str]] = []
    restricted_urls: list[str] = []

    for path in FIRST_PARTY_PATHS:
        page_url = intelligence.normalize_url(urljoin(home, path))
        if not page_url or page_url in seen or not legacy.robots_allows(page_url):
            continue
        seen.add(page_url)
        try:
            response = requests.get(
                page_url,
                timeout=10,
                headers={"User-Agent": intelligence.USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
                allow_redirects=True,
            )
            response.raise_for_status()
            if "html" not in response.headers.get("content-type", "text/html").lower():
                continue
            final_url = intelligence.normalize_url(response.url)
            if not _same_site(home, final_url):
                continue
            page = intelligence.parse_page(final_url, response.text)
        except Exception:
            continue

        if legacy.has_do_not_contact_statement(page.text):
            restricted_urls.append(final_url)
            continue

        for base_score, address, method, label in _publication_candidates(page):
            if growth._is_placeholder_email(address):
                continue
            if any(junk in address for junk in legacy.JUNK_EMAIL_HINTS):
                continue
            if not legacy.is_business_email(address):
                continue
            if not _same_business_domain(home, address):
                continue
            rationale = _role_rationale(page, address, label)
            if not rationale:
                continue
            score = base_score
            if "contact" in urlparse(final_url).path.lower():
                score += 20
            if address.split("@", 1)[0] in COMMERCIAL_ROLE_PREFIXES:
                score += 15
            candidates.append((score, address, final_url, page, method, rationale))

    if restricted_urls:
        return growth.EmailEvidence(
            restricted=True,
            reason="first-party site contains a no-unsolicited-contact statement",
        )
    if not candidates:
        return growth.EmailEvidence(
            reason="no directly published first-party role-relevant business email",
        )

    _score, address, source_url, page, method, rationale = max(
        candidates,
        key=lambda item: (item[0], -len(item[1])),
    )
    observed_at = _utc_now()
    excerpt = f"{growth._email_excerpt(page, address)} | published address: {address}"
    evidence = growth.EmailEvidence(
        email=address,
        source_url=source_url,
        observed_at=observed_at,
        excerpt=excerpt,
        evidence_hash=_evidence_hash(
            address,
            source_url,
            observed_at,
            excerpt,
            method,
            rationale,
        ),
        role_hint=rationale,
        reason=f"direct first-party {method.lower()} publication",
    )
    setattr(evidence, "publication_method", method)
    setattr(evidence, "publication_page_host", _host(source_url))
    setattr(evidence, "publication_first_party", "TRUE")
    setattr(evidence, "publication_role_rationale", rationale)
    setattr(evidence, "no_solicitation_checked_at", observed_at)
    setattr(evidence, "no_solicitation_statement", "NONE_FOUND")
    return evidence


def _compliant_lead_record(*args, **kwargs) -> dict[str, Any]:
    assert _prior_lead_record is not None
    record = _prior_lead_record(*args, **kwargs)
    email_evidence = args[1] if len(args) > 1 else kwargs.get("email_evidence")
    record.update(
        {
            "publication_method": getattr(email_evidence, "publication_method", ""),
            "publication_page_host": getattr(email_evidence, "publication_page_host", ""),
            "publication_first_party": getattr(email_evidence, "publication_first_party", ""),
            "publication_role_rationale": getattr(email_evidence, "publication_role_rationale", ""),
            "no_solicitation_checked_at": getattr(email_evidence, "no_solicitation_checked_at", ""),
            "no_solicitation_statement": getattr(email_evidence, "no_solicitation_statement", ""),
            "initial_outreach_only": "TRUE",
            "commercial_ad_disclosure": "TRUE",
            "compliance_profile": COMPLIANCE_PROFILE,
        }
    )
    return record


def _ensure_one_touch_ledger():
    creds = legacy.Credentials.from_service_account_file(
        legacy.SHEET_CREDS,
        scopes=legacy.SHEET_SCOPES,
    )
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(legacy.SHEET_ID)
    try:
        worksheet = spreadsheet.worksheet(ONE_TOUCH_LEDGER_TAB)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=ONE_TOUCH_LEDGER_TAB,
            rows=2000,
            cols=len(ONE_TOUCH_HEADERS),
        )
        worksheet.append_row(ONE_TOUCH_HEADERS)
    headers = worksheet.row_values(1)
    if not headers:
        worksheet.append_row(ONE_TOUCH_HEADERS)
    else:
        missing = [header for header in ONE_TOUCH_HEADERS if header not in headers]
        if missing:
            worksheet.update("A1", [headers + missing])
    return worksheet


def _load_one_touch_keys(force: bool = False) -> dict[str, set[str]] | None:
    global _one_touch_cache, _one_touch_error
    if _one_touch_cache is not None and not force:
        return _one_touch_cache
    try:
        rows = _ensure_one_touch_ledger().get_all_values()
        headers = rows[0] if rows else ONE_TOUCH_HEADERS
        index = {header: headers.index(header) for header in headers}
        keys = {"emails": set(), "websites": set(), "domains": set(), "names": set()}
        for row in rows[1:]:
            def value(name: str) -> str:
                pos = index.get(name, -1)
                return row[pos].strip() if pos >= 0 and len(row) > pos else ""

            email_address = value("email").lower()
            website = _normalized_site(value("website"))
            domain = value("business_domain").lower() or _host(value("website"))
            name = _normalized_name(value("business_name"))
            if email_address:
                keys["emails"].add(email_address)
            if website:
                keys["websites"].add(website)
            if domain:
                keys["domains"].add(domain)
            if name:
                keys["names"].add(name)
        _one_touch_cache = keys
        _one_touch_error = ""
        return keys
    except Exception as exc:
        _one_touch_cache = None
        _one_touch_error = str(exc)
        print(f"[one-touch] ledger unavailable: {exc}")
        return None


def _one_touch_match(lead: dict[str, Any]) -> tuple[bool, str]:
    keys = _load_one_touch_keys()
    if keys is None:
        return True, f"one-touch ledger unavailable ({_one_touch_error or 'unknown error'})"
    email_address = (lead.get("email") or "").strip().lower()
    website = _normalized_site(lead.get("website") or "")
    domain = _host(lead.get("website") or "") or _email_domain(email_address)
    name = _normalized_name(lead.get("business_name") or "")
    if email_address and email_address in keys["emails"]:
        return True, "recipient email already received initial outreach"
    if website and website in keys["websites"]:
        return True, "business website already received initial outreach"
    if domain and domain in keys["domains"]:
        return True, "business domain already received initial outreach"
    if name and name in keys["names"]:
        return True, "business name already received initial outreach"
    return False, ""


def _find_lead(email_address: str) -> dict[str, Any]:
    normalized = (email_address or "").strip().lower()
    for lead in legacy.get_leads(limit=10000):
        if (lead.get("email") or "").strip().lower() == normalized:
            return lead
    return {}


def _append_one_touch(
    email_address: str,
    business_name: str,
    subject: str,
    source: str,
) -> None:
    global _one_touch_cache
    lead = _find_lead(email_address)
    website = lead.get("website") or ""
    source_url = lead.get("source_url") or ""
    evidence_hash = lead.get("consent_evidence_hash") or ""
    run_id = lead.get("run_id") or ""
    domain = _host(website) or _email_domain(email_address)
    existing, _reason = _one_touch_match(
        {
            "email": email_address,
            "website": website,
            "business_name": business_name,
        }
    )
    if existing and _one_touch_cache is not None:
        return
    try:
        worksheet = _ensure_one_touch_ledger()
        worksheet.append_row(
            [
                email_address.strip().lower(),
                (business_name or "").strip(),
                website,
                domain,
                _utc_now(),
                (subject or "").strip(),
                source_url,
                evidence_hash,
                run_id or source,
            ]
        )
        _one_touch_cache = None
        _load_one_touch_keys(force=True)
    except Exception as exc:
        print(f"[one-touch] ERROR recording business-level send: {exc}")


def _append_ledger_once(
    email_address: str,
    business_name: str,
    subject: str,
    source: str = "bdr_agent",
):
    _append_one_touch(email_address, business_name, subject, source)
    assert _prior_append_ledger is not None
    return _prior_append_ledger(email_address, business_name, subject, source)


def compliant_footer() -> str:
    address = (legacy.SENDER_ADDRESS or "").strip()
    second_contact = (legacy.SENDER_PHONE or legacy.SENDER_WEBSITE or "").strip()
    return (
        "\n\n--\n"
        f"{legacy.SENDER_NAME}\n"
        f"{legacy.SENDER_INDIVIDUAL}\n"
        f"{address}\n"
        f"{second_contact}\n\n"
        "This is a commercial advertisement from Pacific Yew Automations about business automation services.\n"
        f"To opt out, reply UNSUBSCRIBE or email {legacy.REPLY_TO_EMAIL}. "
        "We suppress requests as soon as they are processed and no later than 10 business days."
    )


def _strict_pre_send(lead: dict) -> tuple[bool, str]:
    assert _prior_pre_send is not None
    ok, reason = _prior_pre_send(lead)
    if not ok:
        return ok, reason

    required = {
        "publication_method": {"MAILTO", "VISIBLE_TEXT"},
        "publication_first_party": {"TRUE"},
        "no_solicitation_statement": {"NONE_FOUND"},
        "initial_outreach_only": {"TRUE"},
        "commercial_ad_disclosure": {"TRUE"},
        "compliance_profile": {COMPLIANCE_PROFILE},
    }
    for field, allowed in required.items():
        value = (lead.get(field) or "").strip().upper()
        if value not in {item.upper() for item in allowed}:
            return False, f"missing or invalid compliance evidence: {field}"

    email_address = (lead.get("email") or "").strip().lower()
    website = lead.get("website") or ""
    source_url = lead.get("source_url") or ""
    observed_at = lead.get("consent_observed_at") or ""
    excerpt = lead.get("consent_evidence_excerpt") or ""
    evidence_hash = (lead.get("consent_evidence_hash") or "").strip().lower()
    method = (lead.get("publication_method") or "").strip().upper()
    role_rationale = lead.get("publication_role_rationale") or ""
    checked_at = lead.get("no_solicitation_checked_at") or ""

    if not website or not source_url or not _same_site(website, source_url):
        return False, "publication source is not on the business's own site"
    if not _same_business_domain(website, email_address):
        return False, "recipient address is not on the business's own domain"
    if (lead.get("publication_page_host") or "").strip().lower() != _host(source_url):
        return False, "publication host evidence does not match the source URL"
    if not observed_at or not checked_at or not excerpt or not role_rationale:
        return False, "incomplete publication, restriction, or role evidence"
    if email_address not in excerpt.lower():
        return False, "evidence excerpt does not contain the published recipient address"
    expected_hash = _evidence_hash(
        email_address,
        source_url,
        observed_at,
        excerpt,
        method,
        role_rationale,
    )
    if not re.fullmatch(r"[a-f0-9]{64}", evidence_hash) or evidence_hash != expected_hash:
        return False, "consent evidence hash mismatch"
    if not (lead.get("recipient_role") or "").strip() or not (lead.get("role_relevance") or "").strip():
        return False, "recipient role relevance is not established"

    contacted, contacted_reason = _one_touch_match(lead)
    if contacted:
        return False, contacted_reason
    return True, "ok"


def _delivery_window_ok() -> tuple[bool, str]:
    if os.environ.get("INITIAL_OUTREACH_ONLY", "true").strip().lower() != "true":
        return False, "INITIAL_OUTREACH_ONLY must remain true"
    slot = os.environ.get("BDR_RUN_SLOT", "manual").strip().lower()
    if slot == "manual" and os.environ.get("ALLOW_MANUAL_DELIVERY", "false").strip().lower() != "true":
        return False, "manual delivery is disabled; only four scheduled runs may send"
    expected_minutes = _ALLOWED_SLOTS.get(slot)
    if expected_minutes is None:
        return False, f"unapproved delivery slot: {slot}"

    now = datetime.now(ZoneInfo("America/Vancouver"))
    if now.weekday() >= 5:
        return False, "scheduled cold outreach is limited to weekdays"
    current_minutes = now.hour * 60 + now.minute
    if abs(current_minutes - expected_minutes) > 75:
        return False, f"outside approved business-hour window for slot {slot}"
    return True, "ok"


def _compliant_preflight() -> bool:
    assert _prior_preflight is not None
    if not _prior_preflight():
        return False
    window_ok, reason = _delivery_window_ok()
    if not window_ok:
        print(f"[COMPLIANCE] FAIL: {reason}")
        return False
    if not legacy.SENDER_ADDRESS or not legacy.REPLY_TO_EMAIL:
        print("[COMPLIANCE] FAIL: physical address and reply/unsubscribe address are required.")
        return False
    if _load_one_touch_keys(force=True) is None:
        print("[COMPLIANCE] FAIL: one-touch ledger must be readable before sending.")
        return False
    print("[COMPLIANCE] first-party publication, one-touch, identity, and delivery-window gates ready.")
    return True


def _approve_compliant_drafts() -> int:
    assert _prior_approve is not None
    approved = _prior_approve()
    try:
        worksheet = legacy.get_sheet()
        legacy._sheets_throttle()
        values = worksheet.get_all_values()
        if len(values) < 2 or "status" not in values[0]:
            return approved
        headers = values[0]
        status_index = headers.index("status")
        updates: list[gspread.Cell] = []
        removed = 0
        for row_number, row in enumerate(values[1:], start=2):
            record = dict(zip(headers, row + [""] * (len(headers) - len(row))))
            if (record.get("status") or "").strip().upper() != "APPROVED":
                continue
            ok, reason = _strict_pre_send(record)
            if ok:
                continue
            status = "SENT_DUPLICATE_SKIPPED" if "already received" in reason else "NEEDS_RESEARCH"
            updates.append(gspread.Cell(row=row_number, col=status_index + 1, value=status))
            removed += 1
            print(f"[COMPLIANCE] approval removed for {record.get('email')}: {reason}")
        if updates:
            legacy._sheets_throttle()
            worksheet.update_cells(updates)
            legacy._CONTACTED_CACHE = None
        return max(0, approved - removed)
    except Exception as exc:
        print(f"[COMPLIANCE] approval reconciliation failed closed: {exc}")
        return 0


def install() -> None:
    global _installed, _prior_preflight, _prior_pre_send, _prior_append_ledger
    global _prior_approve, _prior_lead_record
    if _installed:
        return

    for column in COMPLIANCE_COLUMNS:
        if column not in legacy.HEADERS:
            legacy.HEADERS.append(column)
        if column not in growth.GROWTH_COLUMNS:
            growth.GROWTH_COLUMNS.append(column)

    growth.RUN_SLOTS.clear()
    growth.RUN_SLOTS.update(
        {"morning": 0, "late_morning": 1, "midday": 2, "afternoon": 3, "manual": 4}
    )

    _prior_preflight = legacy.preflight_checks
    _prior_pre_send = legacy.pre_send_check
    _prior_append_ledger = legacy._append_to_ledger
    _prior_approve = growth.approve_growth_drafts
    _prior_lead_record = streaming_growth._lead_record

    growth.find_public_business_email = strict_first_party_email
    streaming_growth.find_growth_email = strict_first_party_email
    streaming_growth._lead_record = _compliant_lead_record
    legacy.pre_send_check = _strict_pre_send
    legacy.preflight_checks = _compliant_preflight
    legacy._append_to_ledger = _append_ledger_once
    legacy.casl_footer = compliant_footer
    growth.approve_growth_drafts = _approve_compliant_drafts
    _installed = True
