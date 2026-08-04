from __future__ import annotations

import email as email_module
import imaplib
import os
import random
import re
import smtplib
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from urllib.parse import quote

import bdr_agent as legacy
import growth_engine as growth

BLOCKED_PREFIXES = {
    "abuse", "careers", "compliance", "dmca", "hr", "jobs", "legal",
    "media", "noreply", "no-reply", "press", "privacy", "security", "webmaster",
}
OPT_OUTS = (
    re.compile(r"^\s*(?:unsubscribe|stop)\s*[.!]?\s*$", re.I),
    re.compile(r"\b(?:please\s+)?remove\s+me\b", re.I),
    re.compile(r"\btake\s+me\s+off\b", re.I),
    re.compile(r"\b(?:do\s+not|don['’]?t)\s+(?:email|contact)\s+me\b", re.I),
    re.compile(r"\bno\s+more\s+emails?\b", re.I),
)
QUOTE_BREAKS = (
    re.compile(r"^\s*On .+wrote:\s*$", re.I),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.I),
    re.compile(r"^\s*From:\s+.+$", re.I),
)
HARD_BOUNCE_HINTS = (
    "user unknown", "unknown user", "no such user", "recipient address rejected",
    "mailbox does not exist", "address not found", "domain not found", "invalid recipient",
)
_ORIGINAL_QUERIES = growth.queries_for_slot
_ORIGINAL_FIND_EMAIL = growth.find_public_business_email
_ORIGINAL_UPDATE = legacy.update_lead
_last_send = 0.0


def strip_quoted_reply(text: str) -> str:
    kept = []
    for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.lstrip().startswith(">") or any(p.match(line) for p in QUOTE_BREAKS):
            break
        kept.append(line)
    return "\n".join(kept).strip()


def is_explicit_opt_out(text: str) -> bool:
    fresh = re.sub(r"\s+", " ", strip_quoted_reply(text)).strip()
    return bool(fresh) and any(pattern.search(fresh) for pattern in OPT_OUTS)


def _plain_text(message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in (part.get("Content-Disposition") or "").lower():
                payload = part.get_payload(decode=True)
                if payload is not None:
                    return payload.decode(part.get_content_charset() or "utf-8", "ignore")
        return ""
    payload = message.get_payload(decode=True)
    return payload.decode(message.get_content_charset() or "utf-8", "ignore") if payload else str(message.get_payload() or "")


def safe_scan_unsubscribes() -> int:
    if not (legacy.GMAIL_USER and legacy.GMAIL_APP_PASSWORD):
        return 0
    contacted = set(legacy._lead_emails()) | set(legacy._load_ledger_emails())
    added = 0
    mailbox = None
    try:
        mailbox = imaplib.IMAP4_SSL("imap.zoho.com", 993, timeout=20)
        mailbox.login(legacy.GMAIL_USER, legacy.GMAIL_APP_PASSWORD)
        mailbox.select("INBOX")
        _, messages = mailbox.search(None, "(UNSEEN)")
        for message_id in (messages[0].split() if messages and messages[0] else []):
            _, parts = mailbox.fetch(message_id, "(RFC822)")
            raw = next((part[1] for part in parts if isinstance(part, tuple)), b"")
            if not raw:
                continue
            message = email_module.message_from_bytes(raw)
            sender = parseaddr(message.get("From") or "")[1].strip().lower()
            if sender in contacted and is_explicit_opt_out(_plain_text(message)):
                if legacy.add_to_dnc(sender, reason="unsubscribe", source="imap_explicit_reply"):
                    added += 1
                    print(f"[reply-scan] explicit opt-out: {sender}")
    except Exception as exc:
        print(f"[reply-scan] failed: {exc}")
    finally:
        if mailbox:
            try:
                mailbox.logout()
            except Exception:
                pass
    return added


def is_hard_bounce(text: str) -> bool:
    statuses = re.findall(r"(?im)^Status:\s*([245]\.\d{1,3}\.\d{1,3})", text or "")
    if statuses:
        return any(status.startswith("5.") for status in statuses)
    lowered = (text or "").lower()
    return any(hint in lowered for hint in HARD_BOUNCE_HINTS)


def extract_failed_recipients(text: str, sent_addresses) -> list[str]:
    sent = {address.lower() for address in sent_addresses}
    explicit = re.findall(r"(?im)^(?:Final-Recipient|Original-Recipient):\s*(?:rfc822;)?\s*([^\s;<>]+@[^\s;<>]+)", text or "")
    matched = {address.lower().strip(".>,;") for address in explicit if address.lower().strip(".>,;") in sent}
    return sorted(matched or {address.lower() for address in legacy.EMAIL_RE.findall(text or "") if address.lower() in sent})


def safe_scan_hard_bounces() -> int:
    if not (legacy.GMAIL_USER and legacy.GMAIL_APP_PASSWORD):
        return 0
    sent = legacy._load_ledger_emails()
    added = 0
    mailbox = None
    try:
        mailbox = imaplib.IMAP4_SSL("imap.zoho.com", 993, timeout=20)
        mailbox.login(legacy.GMAIL_USER, legacy.GMAIL_APP_PASSWORD)
        mailbox.select("INBOX")
        _, messages = mailbox.search(None, "(UNSEEN)")
        for message_id in (messages[0].split() if messages and messages[0] else []):
            _, parts = mailbox.fetch(message_id, "(BODY.PEEK[])")
            raw = next((part[1] for part in parts if isinstance(part, tuple)), b"")
            text = raw.decode("utf-8", "ignore")
            if not raw or not is_hard_bounce(text):
                continue
            for address in extract_failed_recipients(text, sent):
                if legacy.add_to_dnc(address, reason="hard_bounce", source="imap_dsn_5xx"):
                    added += 1
                    print(f"[bounce] permanent failure: {address}")
    except Exception as exc:
        print(f"[bounce] scan failed: {exc}")
    finally:
        if mailbox:
            try:
                mailbox.logout()
            except Exception:
                pass
    return added


def permissive_preflight() -> bool:
    ok = bool(legacy.SENDER_ADDRESS and legacy.GMAIL_USER and legacy.GMAIL_APP_PASSWORD)
    if not ok:
        print("[PREFLIGHT] FAIL: sender identity or Zoho credentials missing.")
    try:
        with smtplib.SMTP_SSL(legacy.SMTP_HOST, 465, timeout=15) as smtp:
            smtp.login(legacy.GMAIL_USER, legacy.GMAIL_APP_PASSWORD)
        print("[PREFLIGHT] SMTP auth OK.")
    except Exception as exc:
        print(f"[PREFLIGHT] FAIL SMTP: {exc}")
        ok = False
    try:
        mailbox = imaplib.IMAP4_SSL("imap.zoho.com", 993, timeout=15)
        mailbox.login(legacy.GMAIL_USER, legacy.GMAIL_APP_PASSWORD)
        mailbox.select("INBOX")
        mailbox.logout()
        print("[PREFLIGHT] IMAP auth OK.")
    except Exception as exc:
        print(f"[PREFLIGHT] FAIL IMAP: {exc}")
        ok = False
    try:
        headers = legacy.get_sheet().row_values(1)
        missing = sorted(set(legacy.HEADERS) - set(headers))
        duplicates = sorted({header for header in headers if headers.count(header) > 1})
        if missing or duplicates:
            print(f"[PREFLIGHT] FAIL Sheet missing={missing} duplicates={duplicates}")
            ok = False
        else:
            print("[PREFLIGHT] Sheet OK; extra columns and historical order accepted.")
    except Exception as exc:
        print(f"[PREFLIGHT] FAIL Sheet: {exc}")
        ok = False
    print(f"[PREFLIGHT] {'ALL GREEN' if ok else 'FAILED'}")
    return ok


def recipient_allowed(address: str) -> bool:
    prefix = address.rsplit("@", 1)[0].lower() if "@" in (address or "") else ""
    return bool(prefix) and prefix not in BLOCKED_PREFIXES


def build_message(to_email: str, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{legacy.SENDER_NAME} <{legacy.GMAIL_USER}>"
    message["To"] = to_email
    message["Reply-To"] = legacy.REPLY_TO_EMAIL or legacy.GMAIL_USER
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=(legacy.GMAIL_USER or "pacificyew.pro").split("@")[-1])
    message["List-Unsubscribe"] = f"<mailto:{legacy.REPLY_TO_EMAIL}?subject={quote('UNSUBSCRIBE')}>"
    message.set_content(body + legacy.casl_footer())
    return message


def paced_send_email(to_email: str, subject: str, body: str) -> str:
    global _last_send
    if not recipient_allowed(to_email):
        return f"ERROR: protected/non-commercial role inbox ({to_email})."
    minimum = max(0.0, float(os.environ.get("SEND_MIN_INTERVAL_SECONDS", "12")))
    jitter = max(0.0, float(os.environ.get("SEND_JITTER_SECONDS", "8")))
    gap = minimum + random.uniform(0, jitter)
    elapsed = time.monotonic() - _last_send if _last_send else gap
    if elapsed < gap:
        time.sleep(gap - elapsed)
    try:
        with smtplib.SMTP_SSL(legacy.SMTP_HOST, 465, timeout=30) as smtp:
            smtp.login(legacy.GMAIL_USER, legacy.GMAIL_APP_PASSWORD)
            smtp.send_message(build_message(to_email, subject, body))
        _last_send = time.monotonic()
        return "SENT"
    except Exception as exc:
        return f"Error sending: {exc}"


def route_offer(dossier: str) -> str:
    text = (dossier or "").lower()
    scores = {
        "intake_and_billing_admin": 3 * sum(text.count(x) for x in ("insurance", "direct billing", "intake form", "financing")),
        "lead_response_and_estimates": 3 * sum(text.count(x) for x in ("request a quote", "quote or estimate", "emergency", "after-hours")),
        "booking_and_no_show": 2 * sum(text.count(x) for x in ("online booking", "appointment", "no-show", "schedule")),
        "retention_and_reactivation": 2 * sum(text.count(x) for x in ("membership", "maintenance plan", "recurring", "follow-up")),
        "operations_workflow_audit": 1,
    }
    priority = {"intake_and_billing_admin": 4, "lead_response_and_estimates": 3, "booking_and_no_show": 2, "retention_and_reactivation": 1, "operations_workflow_audit": 0}
    return max(scores, key=lambda route: (scores[route], priority[route]))


def manual_safe_queries(slot=None, *, day_of_year=None, query_count=None):
    slot = slot or growth._run_slot()
    if slot != "manual":
        return _ORIGINAL_QUERIES(slot, day_of_year=day_of_year, query_count=query_count)
    count = min(query_count or int(os.environ.get("QUERIES_PER_RUN", legacy.QUERIES_PER_RUN)), len(legacy.SEARCH_QUERIES))
    day = day_of_year or datetime.now(timezone.utc).timetuple().tm_yday
    used = set()
    for scheduled in ("overnight", "morning", "midday", "afternoon"):
        used.update(_ORIGINAL_QUERIES(scheduled, day_of_year=day, query_count=count))
    start = int(os.environ.get("GITHUB_RUN_ID", str(int(time.time())))) % len(legacy.SEARCH_QUERIES)
    result = []
    for offset in range(len(legacy.SEARCH_QUERIES)):
        candidate = legacy.SEARCH_QUERIES[(start + offset) % len(legacy.SEARCH_QUERIES)]
        if candidate not in used and candidate not in result:
            result.append(candidate)
        if len(result) == count:
            break
    return result


def hardened_find_email(website_url: str):
    evidence = _ORIGINAL_FIND_EMAIL(website_url)
    if evidence.email and not recipient_allowed(evidence.email):
        return growth.EmailEvidence(reason="protected/non-commercial role inbox")
    return evidence


def _parse_iso(value: str):
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def effective_send_limit(requested: int) -> int:
    today = datetime.now(timezone.utc).date()
    try:
        rows = legacy._ensure_ledger().get_all_values()
        headers = rows[0] if rows else []
        index = headers.index("sent_at") if "sent_at" in headers else -1
        sent_today = sum(1 for row in rows[1:] if index >= 0 and len(row) > index and _parse_iso(row[index]) and _parse_iso(row[index]).date() == today)
    except Exception as exc:
        print(f"[delivery-health] ledger read failed: {exc}")
        sent_today = 0
    daily_cap = max(1, int(os.environ.get("DAILY_SEND_CAP", "48")))
    effective = min(requested, max(0, daily_cap - sent_today))
    print(f"[delivery-health] requested={requested} effective={effective} sent_today={sent_today}/{daily_cap}")
    return effective


def audited_update(row_id: int, fields: dict) -> None:
    updated = dict(fields)
    if str(updated.get("status", "")).upper() == "RESERVED":
        updated.setdefault("reservation_started_at", datetime.now(timezone.utc).isoformat())
    _ORIGINAL_UPDATE(row_id, updated)


def install() -> None:
    growth.install()
    if "reservation_started_at" not in legacy.HEADERS:
        legacy.HEADERS.append("reservation_started_at")
    legacy.scan_unsubscribes = safe_scan_unsubscribes
    growth.scan_hard_bounces = safe_scan_hard_bounces
    legacy.preflight_checks = permissive_preflight
    legacy.send_email = paced_send_email
    legacy.update_lead = audited_update
    growth.queries_for_slot = manual_safe_queries
    growth.find_public_business_email = hardened_find_email
    growth.route_offer = route_offer


def main() -> None:
    install()
    requested = max(1, int(os.environ.get("SEND_LIMIT", str(legacy.SEND_LIMIT))))
    effective = effective_send_limit(requested)
    if effective <= 0:
        print("[delivery-health] daily cap reached; no send attempted.")
        return
    os.environ["SEND_LIMIT"] = str(effective)
    growth.main()


if __name__ == "__main__":
    main()
