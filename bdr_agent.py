import os
import re
import time
import smtplib
import imaplib
import requests
import email
from email.message import EmailMessage
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()  # load .env when run standalone (app.py also loads it)

# Google Sheets
import gspread
from google.oauth2.service_account import Credentials

# --- Lazy globals ---
_or_key: str = None
_or_model: str = None
_sheet = None
_last_scraped_source_url: str = ""  # audit trail: URL where the last email was found

# Default to a FREE OpenRouter model. Override with OPENROUTER_MODEL in .env.
DEFAULT_MODEL = "tencent/hy3:free"

OR_REFERER = os.environ.get("OR_REFERER", "https://pacificyew.pro")
OR_TITLE = os.environ.get("OR_TITLE", "Pacific Yew BDR")

PACIFIC_YEW_ICP = """
Target: Lower Mainland BC service businesses (trades, clinics, law firms, professional services).
Pain Point: Drowning in manual admin, using basic booking/CRM tools (or none), missing follow-ups.
Our Solution: AI automation + "Relationship Intelligence OS" (we build the internal data graph, not Zapier wrappers).
Tone: Quiet Operator. Professional, direct, zero hype.
"""

# --- Google Sheets config (storage) ---
SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", "Pacific Yew Outbound CRM")
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "1yOvQ4kLabFwZaD2RRlPcTzRJbz2ZakHipbZsADqzI1k")
SHEET_CREDS = os.environ.get("GOOGLE_SHEET_CREDS", "creds/service_account.json")
SHEET_TAB = os.environ.get("GOOGLE_SHEET_TAB", "Leads")
SHEET_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# --- Sending/reply mailbox (Zoho Mail) ---
# GMAIL_USER / GMAIL_APP_PASSWORD now hold the Zoho credentials
# (contact@pacificyew.pro + app-specific password). Replies — including
# UNSUBSCRIBE — land here, and the CASL scanner polls this same Zoho inbox
# via IMAP (imap.zoho.com:993). One mailbox, send + scan.
GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
REPLY_TO_EMAIL = os.environ.get("REPLY_TO_EMAIL", GMAIL_USER or "contact@pacificyew.pro")

# --- Sender identity (REQUIRED for CASL-compliant sending) ---
# CASL s.6: every CEM must identify BOTH the business AND the individual sender,
# a physical mailing address, and at least one other contact method.
SENDER_NAME = os.environ.get("SENDER_NAME", "Pacific Yew Automations")
SENDER_INDIVIDUAL = os.environ.get("SENDER_INDIVIDUAL", "Michael Goulden")  # the natural person sending
SENDER_ADDRESS = os.environ.get("SENDER_ADDRESS", "")  # physical mailing address — REQUIRED by CASL
SENDER_WEBSITE = os.environ.get("SENDER_WEBSITE", "https://pacificyew.pro")
SENDER_PHONE = os.environ.get("SENDER_PHONE", "")  # optional 2nd contact method for the footer
SEND_LIMIT = int(os.environ.get("SEND_LIMIT", "20"))  # max emails per run (Gmail-safe)

# Column order for the Leads tab. New columns are APPENDED so existing rows stay aligned.
# Compliance columns (source_url, consent_type, dnc_timestamp, dnc_processed) are
# appended at the end so pre-existing data keeps its original column mapping.
HEADERS = [
    "business_name", "website", "phone", "email", "agent_analysis",
    "status", "created_at", "email_subject", "email_body", "sent_at",
    "source_url", "consent_type", "dnc_timestamp", "dnc_processed",
]


# ─── OpenRouter (drafting) ────────────────────────────────────────────────────
def _get_openrouter():
    global _or_key, _or_model
    if _or_key is None:
        _or_key = os.environ.get("OPENROUTER_API_KEY")
        _or_model = os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)
    return _or_key, _or_model


def _or_chat(system_prompt, user_prompt, temperature=0.7):
    """Single OpenRouter chat call with retry/backoff. Returns content str or None."""
    api_key, model = _get_openrouter()
    if not api_key:
        return None
    last_err = None
    for attempt in range(4):  # 1 initial + 3 retries
        try:
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "HTTP-Referer": OR_REFERER,
                    "X-Title": OR_TITLE,
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": temperature,
                },
                timeout=90,
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except Exception as e:
            last_err = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (429, 500, 502, 503, 504):
                wait = 2 ** attempt + 1
                print(f"OpenRouter transient error ({status}); retrying in {wait}s...")
                time.sleep(wait)
                continue
            print(f"OpenRouter API error: {e}")
            break
    print(f"OpenRouter failed after retries: {last_err}")
    return None


def qualify_and_draft(business):
    """Legacy: returns a free-text qualify+draft blob (kept for app.py compatibility)."""
    website_text = scrape_website(business.get("website", ""))
    system_prompt = f"You are a fractional COO and BDR for Pacific Yew (pacificyew.pro).\n{PACIFIC_YEW_ICP}"
    user_prompt = f"""
    Business Data: {business}
    Website Text: {website_text}

    Task 1: Qualify. Is this a good fit for a $10k-$15k Internal OS package? (Yes/No and why).
    Task 2: Draft. If yes, write a 4-sentence cold email to {business.get('title')}.
    """
    out = _or_chat(system_prompt, user_prompt)
    return out or "Error generating draft."


def analyze_and_draft(business, website_text):
    """One LLM call that returns a dict: {qualified, subject, body}.
    Parses a strict format so we can store a clean, send-ready subject + body."""
    system_prompt = (
        "You are the BDR for Pacific Yew (pacificyew.pro), a SOFTWARE AUTOMATION agency in "
        "Vancouver, BC. We build AI automations (booking reminders, follow-ups, client records, "
        "invoicing) for local service businesses like HVAC, plumbers, clinics and law firms.\n"
        "CRITICAL: 'Pacific Yew' is our company NAME only. It is NOT about wood, lumber, trees, "
        "carpentry or timber. NEVER mention wood, yew trees, lumber, sawmills or carpentry. "
        "We sell software automation, nothing physical.\n"
        "Voice rules — follow these exactly:\n"
        "- Write like a sharp local operator talking to a busy business owner, not a tech vendor.\n"
        "- NO jargon. Never use 'data graph', 'relationship intelligence', 'internal OS', "
        "'architecture', or 'fractional COO'. Say what it DOES in plain words.\n"
        "- Speak in first person plural ('we' / 'our') — a person writing, not a machine.\n"
        "- Short. 3-4 sentences. One clear, low-pressure ask.\n"
        "- Confident but never hypey or salesy. No exclamation marks. No spammy subject lines.\n"
        "- NO invented facts. Do NOT claim to have worked with them, name their clients, cite "
        "their numbers, or state anything not present in the website text below. If the website "
        "text is thin, speak only to the well-known operational pain of their TRADE (no-shows, "
        "double-bookings, chasing late payments, client details scattered across texts/email) — "
        "these are real industry-wide problems, not claims about this specific business.\n"
        "- Tailor the value prop to THIS business: lead with the pain most relevant to how THEY "
        "actually operate (read their site), then say plainly what we'd set up, then the payoff.\n"
    )
    user_prompt = f"""
Business: {business.get('title')}
Website: {business.get('website')}
Website Text (excerpt): {website_text[:3000]}

Write ONE cold outreach email. Return EXACTLY this format, nothing else:

QUALIFIED: <Yes or No, plus a 1-line reason>
SUBJECT: <a specific, plain subject under 60 chars — reference their trade or a concrete pain, no clickbait>
BODY:
<3-4 sentence email, first-person plural voice.>
- Sentence 1: a relevant opening tied to how a business in THEIR trade actually runs day to day. Only reference a SPECIFIC fact (service offered, area, hours) if it appears in the website text above — otherwise keep it trade-general, never invent.
- Sentence 2: what we do in plain language (e.g. put their booking reminders, follow-ups and client records on autopilot so nothing slips through). Vary the wording — do not reuse a fixed phrase across emails.
- Sentence 3: the concrete payoff for them (fewer no-shows, less admin drag, more jobs booked). Grounded, not a promise of miracles.
- Sentence 4: a low-pressure ask (a 15-minute call, or offer to send a short walkthrough). End on that.
Do NOT include a greeting name you don't know, a signature, or a footer.>
"""
    out = _or_chat(system_prompt, user_prompt) or ""
    # Retry once if the model dropped the BODY section (format flake).
    if "BODY:" not in out:
        out = _or_chat(
            system_prompt,
            user_prompt + "\n\nIMPORTANT: you MUST output all three sections "
            "(QUALIFIED:, SUBJECT:, BODY:). Do not stop early.",
        ) or out
    qualified = _extract(out, r"QUALIFIED:\s*(.+?)(?:\n|$)")
    subject = _extract(out, r"SUBJECT:\s*(.+?)(?:\n|$)")
    body_m = re.search(r"BODY:\s*(.+?)(?:>?\s*$)", out, re.S | re.I)
    body = (body_m.group(1).strip().strip(">").strip() if body_m else "")
    if not subject:
        subject = f"Quick idea for {business.get('title', 'your team')}"
    return {"qualified": qualified or out.strip()[:300], "subject": subject.strip(), "body": body}


def _extract(text, pattern):
    m = re.search(pattern, text, re.I)
    return m.group(1).strip() if m else ""


# ─── Discovery (free, key-less web search) ──────────────────────────────────
def discover_businesses(search_query: str = "wealth management firm Vancouver"):
    apify_actor = os.environ.get("APIFY_ACTOR")
    if apify_actor and os.environ.get("APIFY_TOKEN"):
        return _discover_apify(search_query, apify_actor)
    return _discover_ddg(search_query)


def _discover_ddg(search_query: str):
    try:
        url = "https://html.duckduckgo.com/html/"
        resp = requests.post(url, data={"q": search_query}, timeout=20,
                             headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            print(f"DuckDuckGo error: {resp.status_code}")
            return []
        results = []
        blocks = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', resp.text, re.S)
        seen = set()
        for href, title_html in blocks:
            title = re.sub(r"<[^>]+>", "", title_html).strip()
            m = re.search(r"uddg=([^&]+)", href)
            site = requests.utils.unquote(m.group(1)) if m else href
            if not site.startswith("http"):
                continue
            domain = re.sub(r"^https?://(www\.)?", "", site).split("/")[0]
            if domain in seen or any(x in domain for x in ("duckduckgo", "wikipedia", "youtube", "facebook", "instagram", "linkedin")):
                continue
            seen.add(domain)
            results.append({"title": title or domain, "website": site, "phone": "", "email": ""})
            if len(results) >= 5:
                break
        print(f"Discovered {len(results)} businesses via web search.")
        return results
    except Exception as e:
        print(f"Discovery error: {e}")
        return []


def _discover_apify(search_query: str, actor: str):
    token = os.environ.get("APIFY_TOKEN")
    api_url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token={token}"
    payload = {"searchStrings": [search_query], "maxCrawledPlacesPerSearch": 5}
    response = requests.post(api_url, json=payload, timeout=120)
    if response.status_code == 200:
        return response.json()
    print(f"Apify error: {response.status_code} - {response.text}")
    return []


# ─── Directory filter (drop "Top 10..."/Yelp junk, keep real businesses) ─────
DIRECTORY_DOMAINS = (
    "yelp.", "yably.", "cdncompanies.", "bbb.org", "threebestrated", "expertise.com",
    "clutch.co", "yellowpages", "google.com", "604list", "wealthbureau", "houzz",
    "tripadvisor", "glassdoor", "indeed.", "trustpilot", "ratemds", "opencare",
)
DIRECTORY_TITLE_HINTS = ("best ", "top 10", "top10", "10 best", "directory", " reviews", "recommendation")


def is_directory(biz) -> bool:
    d = (biz.get("website") or "").lower()
    t = (biz.get("title") or "").lower()
    if any(x in d for x in DIRECTORY_DOMAINS):
        return True
    if any(x in t for x in DIRECTORY_TITLE_HINTS):
        return True
    return False


# ─── Scraping (website text + contact email) ─────────────────────────────────
from urllib.parse import urlparse
try:
    from urllib.robotparser import RobotFileParser
except Exception:  # pragma: no cover
    RobotFileParser = None


# ─── robots.txt respect (report §5: no-scrape rule) ─────────────────────────
# If a site's robots.txt disallows scraping, we must not harvest it — doing so
# voids the CASL "conspicuous publication" defense and risks civil tort.
_robots_cache = {}


def robots_allows(url: str) -> bool:
    """Return True if robots.txt permits fetching this URL. Defaults to True on
    any error/timeout (fail-open so legitimate public pages still work)."""
    if RobotFileParser is None or not url:
        return True
    try:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        rp = _robots_cache.get(base)
        if rp is None:
            rp = RobotFileParser()
            rp.set_url(base + "/robots.txt")
            rp.read()
            _robots_cache[base] = rp
        return rp.can_fetch("*", url)
    except Exception:
        return True


def scrape_website(website_url):
    try:
        if not website_url.startswith("http"):
            website_url = "https://" + website_url
        if not robots_allows(website_url):
            print(f"  [robots.txt] disallowed: {website_url} — skipping scrape.")
            return "No website text available."
        response = requests.get(website_url, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
        text = re.sub(r"<[^>]+>", " ", response.text)
        return re.sub(r"\s+", " ", text)[:5000]
    except Exception:
        return "No website text available."


EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
JUNK_EMAIL_HINTS = ("example.com", "sentry", "wixpress", "godaddy", ".png", ".jpg",
                    ".jpeg", ".gif", "@2x", "yourdomain", "domain.com", "email.com",
                    "squarespace", "schema.org", "w3.org")
PREFERRED_PREFIXES = ("info@", "contact@", "hello@", "admin@", "office@", "reception@", "clinic@")

# ─── PIPA "Business Contact Information" guard (report §2 THE PIPA TRAP) ──────
# BC PIPA: collecting a PERSONAL email (gmail/yahoo/shaw/telus...) for marketing
# without consent is a PIPA violation. Only BUSINESS-domain emails are exempt BCI.
# Free/webmail providers are treated as personal regardless of where they appear.
FREE_EMAIL_DOMAINS = (
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com",
    "shaw.ca", "telus.net", "telus.com", "rogers.com", "bell.net", "bell.ca",
    "fido.ca", "windmobile.ca", "fizz.ca", "videotron.ca", "cox.net", "comcast.net",
    "proton.me", "protonmail.com", "tuta.io", "gmx.com", "mail.com", "zoho.com",
    "yandex.com", "yandex.ru", "disroot.org",
)


def is_business_email(email: str) -> bool:
    """True only if the address is on a business/corporate domain (PIPA-exempt BCI).

    Free/webmail providers are classified as personal information under PIPA and
    must NOT be collected/stored for cold outreach without express consent.
    """
    if not email or "@" not in email:
        return False
    domain = email.lower().split("@")[-1]
    if domain in FREE_EMAIL_DOMAINS:
        return False
    return True


def scrape_email(website_url) -> str:
    """Fetch homepage + common contact pages, extract the best BUSINESS email.

    Returns '' if none. Applies the PIPA guard: free/webmail personal addresses
    are rejected (report §2). Also rejects sites whose contact page carries an
    explicit 'do not contact' statement (CASL implied-consent is void there).

    To preserve the audit trail (CASL Pillar A — proof of conspicuous
    publication), the exact URL the email was found on is recorded in
    `_last_scraped_source_url` (read by discover_and_draft into source_url).
    """
    global _last_scraped_source_url
    _last_scraped_source_url = ""
    if not website_url:
        return ""
    if not website_url.startswith("http"):
        website_url = "https://" + website_url
    base = website_url.rstrip("/")
    candidates = []
    source_page = ""
    for path in ("", "/contact", "/contact-us", "/about", "/about-us"):
        page_url = base + path
        if not robots_allows(page_url):
            continue  # respect robots.txt — do not harvest disallowed paths
        try:
            r = requests.get(base + path, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
            text = r.text
            for e in EMAIL_RE.findall(text):
                el = e.lower().strip(".")
                if any(j in el for j in JUNK_EMAIL_HINTS):
                    continue
                if not is_business_email(el):
                    # Personal/webmail address — skip per PIPA (do not collect).
                    continue
                candidates.append(el)
                if not source_page:
                    source_page = base + path
            # Pillar A guard: if this page explicitly refuses cold contact, bail.
            if has_do_not_contact_statement(text):
                print(f"  [PIPA/CASL] {base}{path} states 'do not contact' — skipping.")
                return ""
        except Exception:
            continue
        if candidates:
            break  # stop at the first page that yields a business email
    if not candidates:
        return ""
    _last_scraped_source_url = source_page or base
    for pref in PREFERRED_PREFIXES:
        for c in candidates:
            if c.startswith(pref):
                return c
    return sorted(set(candidates), key=len)[0]


# ─── Google Sheets (storage) ─────────────────────────────────────────────────
def get_sheet():
    """Lazy-open the Leads worksheet; reconcile the header to HEADERS (adds new columns)."""
    global _sheet
    if _sheet is None:
        creds = Credentials.from_service_account_file(SHEET_CREDS, scopes=SHEET_SCOPES)
        client = gspread.authorize(creds)
        sh = client.open_by_key(SHEET_ID)
        try:
            _sheet = sh.worksheet(SHEET_TAB)
        except gspread.WorksheetNotFound:
            _sheet = sh.add_worksheet(title=SHEET_TAB, rows=1000, cols=len(HEADERS))
        existing = _sheet.row_values(1)
        if not existing:
            _sheet.append_row(HEADERS)
        else:
            # Append only MISSING columns to the end — never reorder existing
            # ones, so pre-existing data stays aligned to its original columns.
            missing = [h for h in HEADERS if h not in existing]
            if missing:
                new_header = existing + missing
                if _sheet.col_count < len(new_header):
                    _sheet.add_cols(len(new_header) - _sheet.col_count)
                _sheet.update("A1", [new_header])
    return _sheet


def get_leads(limit: int = 50):
    ws = get_sheet()
    vals = ws.get_all_values()
    if len(vals) < 2:
        return []
    header = vals[0]
    out = []
    for i, row in enumerate(vals[1:], start=2):
        rec = dict(zip(header, row + [""] * (len(header) - len(row))))
        rec["_row"] = i
        out.append(rec)
    return list(reversed(out))[:limit]


def dedup_exists(website: str) -> bool:
    if not website:
        return False
    ws = get_sheet()
    vals = ws.get_all_values()
    if len(vals) < 2 or "website" not in vals[0]:
        return False
    col = vals[0].index("website")
    target = website.strip().lower()
    return any(r[col].strip().lower() == target for r in vals[1:] if len(r) > col)


def insert_lead(data: dict):
    """Append a single lead row (status defaults to DRAFT_READY)."""
    data = dict(data)
    data.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    row = [data.get(h, "") for h in HEADERS]
    get_sheet().append_row(row)


def _reconcile_header(ws):
    """Ensure A1 contains the full current HEADERS (appends any missing cols)."""
    existing = ws.row_values(1)
    if not existing:
        ws.append_row(HEADERS)
        return HEADERS
    missing = [h for h in HEADERS if h not in existing]
    if missing:
        new_header = existing + missing
        if ws.col_count < len(new_header):
            ws.add_cols(len(new_header) - ws.col_count)
        ws.update("A1", [new_header])
        return new_header
    return existing


def insert_leads_batch(rows: list):
    """Append many lead rows TRUSTWORTHILY.

    The previous append_rows() path silently dropped rows under load
    (reported N written, persisted far fewer) due to a Sheets append race +
    eventual consistency. This version reads the live sheet, rebuilds the full
    table, and overwrites it atomically, then VERIFIES the new row count before
    returning. Returns the count actually persisted (never a phantom number).
    """
    if not rows:
        return 0
    ws = get_sheet()
    header = _reconcile_header(ws)
    now = datetime.now(timezone.utc).isoformat()
    sheet_rows = []
    for data in rows:
        data = dict(data)
        data.setdefault("created_at", now)
        sheet_rows.append([data.get(h, "") for h in header])
    # Rebuild full table: existing data rows + new ones.
    existing = ws.get_all_values()
    existing_data = existing[1:] if len(existing) > 1 else []
    new_table = existing + sheet_rows
    # Ensure table width matches header width (pad short rows).
    width = len(header)
    new_table = [r + [""] * (width - len(r)) for r in new_table]
    target = len(new_table)
    # Write the whole table back atomically.
    ws.update("A1", new_table, value_input_option="USER_ENTERED")
    # Verify what actually persisted (read back).
    for _ in range(3):
        persisted = len(ws.get_all_values())
        if persisted >= target:
            break
        time.sleep(1.5)
    actual_new = persisted - len(existing)
    return max(0, actual_new)


def update_lead(row_id: int, fields: dict):
    ws = get_sheet()
    header = ws.row_values(1)
    for k, v in fields.items():
        if k in header:
            ws.update_cell(row_id, header.index(k) + 1, v)


# ─── Do-Not-Contact (honors CASL unsubscribe requests) ───────────────────────
# The authoritative DNC list lives on the Sheet's "Do Not Contact" tab so it
# PERSISTS across GitHub Actions runs (the DNC_EMAILS env var resets each run).
# The unsubscribe scanner appends to that tab; send_email + send_approved both
# refuse to email anyone on it. DNC_EMAILS (env) is a manual override fallback.
DNC_TAB = os.environ.get("DNC_TAB", "Do Not Contact")
DNC_EMAILS = [e.strip().lower() for e in os.environ.get("DNC_EMAILS", "").split(",") if e.strip()]
# In-memory cache so repeated calls in one run don't re-read the Sheet every time.
_BLOCKED_CACHE = None


def get_dnc_worksheet():
    """Lazy-open (creating if needed) the persistent Do Not Contact tab."""
    creds = Credentials.from_service_account_file(SHEET_CREDS, scopes=SHEET_SCOPES)
    client = gspread.authorize(creds)
    sh = client.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(DNC_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=DNC_TAB, rows=1000, cols=4)
        ws.append_row(["email", "reason", "added_at", "source"])
    # ensure header exists
    if not ws.row_values(1):
        ws.append_row(["email", "reason", "added_at", "source"])
    return ws


def add_to_dnc(email: str, reason: str = "unsubscribe", source: str = "scanner"):
    """Persist an address to the Sheet DNC tab. Idempotent (no dupes)."""
    email = (email or "").strip().lower()
    if not email:
        return False
    ws = get_dnc_worksheet()
    existing = {r[0].strip().lower() for r in ws.get_all_values()[1:] if r and r[0].strip()}
    if email in existing:
        return False
    ts = datetime.now(timezone.utc).isoformat()
    ws.append_row([email, reason, ts, source])
    # Keep the in-run cache coherent so a just-added address is honored immediately
    # (e.g. scanner adds it, then send_approved's is_blocked must see it this run).
    global _BLOCKED_CACHE
    if _BLOCKED_CACHE is not None:
        _BLOCKED_CACHE.add(email)
    _mark_lead_do_not_contact(email, dnc_timestamp=ts)
    return True


def _blocked_set() -> set:
    """Union of env DNC + persistent Sheet DNC tab (cached per run)."""
    global _BLOCKED_CACHE
    if _BLOCKED_CACHE is not None:
        return _BLOCKED_CACHE
    blocked = set(DNC_EMAILS)
    try:
        ws = get_dnc_worksheet()
        for r in ws.get_all_values()[1:]:
            if r and r[0].strip():
                blocked.add(r[0].strip().lower())
    except Exception as e:
        print(f"[DNC] Could not read Sheet DNC tab ({e}); using env DNC only.")
    _BLOCKED_CACHE = blocked
    return blocked


def is_blocked(to_addr: str) -> bool:
    return to_addr.strip().lower() in _blocked_set()


def _lead_emails() -> set:
    """Lowercased set of every email we have on a lead (people we may have mailed)."""
    try:
        ws = get_sheet()
        vals = ws.get_all_values()
        if len(vals) < 2 or "email" not in vals[0]:
            return set()
        col = vals[0].index("email")
        return {r[col].strip().lower() for r in vals[1:] if len(r) > col and r[col].strip()}
    except Exception:
        return set()


def scan_unsubscribes() -> int:
    """Poll the Zoho Inbox for UNSUBSCRIBE/STOP replies and honor CASL opt-outs.

    CRITICAL SCOPE GUARD: we only act on senders who are ACTUAL LEADS we
    contacted (present in the Leads tab). A generic inbox is full of
    newsletters/promos whose bodies contain 'unsubscribe'/'stop' — those are NOT
    opt-outs from our outreach and must never pollute the DNC list. CASL only
    requires honoring unsubscribe requests from people we actually emailed.
    Zoho IMAP (pivot from Gmail): host imap.zoho.com, creds = GMAIL_USER/PW.
    """
    host = "imap.zoho.com"
    user = GMAIL_USER
    pw = GMAIL_APP_PASSWORD
    if not (user and pw):
        print("[scan] Zoho IMAP creds (GMAIL_USER/GMAIL_APP_PASSWORD) not set — skipping.")
        return 0
    try:
        mail = imaplib.IMAP4_SSL(host, 993)
        mail.login(user, pw)
        mail.select("inbox")
    except Exception as e:
        print(f"[scan] Zoho IMAP login failed ({e}) — skipping unsubscribe scan.")
        return 0

    try:
        status, messages = mail.search(None, "(UNSEEN)")
        email_ids = messages[0].split() if messages and messages[0] else []
    except Exception as e:
        print(f"[scan] Zoho IMAP search failed ({e})")
        try:
            mail.logout()
        except Exception:
            pass
        return 0

    leads = _lead_emails()  # only these addresses can trigger a real opt-out
    added = 0
    for e_id in email_ids:
        try:
            _, msg_data = mail.fetch(e_id, "(RFC822)")
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    email_body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                payload = part.get_payload(decode=True)
                                email_body = payload.decode("utf-8", "ignore") if payload else ""
                                break
                    else:
                        payload = msg.get_payload(decode=True)
                        email_body = payload.decode("utf-8", "ignore") if payload else ""

                    if "unsubscribe" in email_body.lower() or "stop" in email_body.lower():
                        sender_raw = msg.get("From") or ""
                        sender_email = sender_raw.split("<")[-1].split(">")[0].strip().lower()
                        if sender_email not in leads:
                            # Not someone we contacted — ignore inbox noise (newsletters, etc.)
                            print(f"[scan] ignoring non-lead sender {sender_email} (not a Pacific Yew contact).")
                            continue
                        if add_to_dnc(sender_email, reason="unsubscribe", source="imap_scan"):
                            added += 1
                            _mark_lead_do_not_contact(sender_email)
                            print(f"Added to DNC: {sender_email}")
        except Exception as e:
            print(f"[scan] error processing message {e_id}: {e}")
    try:
        mail.logout()
    except Exception:
        pass
    print(f"[scan] unsubscribe scan done — {added} new opt-out(s) honored.")
    return added


def _mark_lead_do_not_contact(email: str, dnc_timestamp: str = ""):
    """If a lead row has this email, mark it DO_NOT_CONTACT + stamp dnc audit cols."""
    try:
        ws = get_sheet()
        vals = ws.get_all_values()
        if len(vals) < 2 or "email" not in vals[0]:
            return
        col = vals[0].index("email")
        status_col = vals[0].index("status") if "status" in vals[0] else None
        ts_col = vals[0].index("dnc_timestamp") if "dnc_timestamp" in vals[0] else None
        proc_col = vals[0].index("dnc_processed") if "dnc_processed" in vals[0] else None
        for i, r in enumerate(vals[1:], start=2):
            if len(r) > col and r[col].strip().lower() == email:
                if status_col is not None:
                    ws.update_cell(i, status_col + 1, "DO_NOT_CONTACT")
                if ts_col is not None and dnc_timestamp:
                    ws.update_cell(i, ts_col + 1, dnc_timestamp)
                if proc_col is not None:
                    ws.update_cell(i, proc_col + 1, "TRUE")
                print(f"[scan] marked lead row {i} DO_NOT_CONTACT (dnc stamped).")
    except Exception as e:
        print(f"[scan] could not mark lead for {email}: {e}")


# ─── Gmail (sending) ────────────────────────────────────────────────────────
def casl_footer() -> str:
    """CASL-compliant footer — EXACT block (report §4). Hardcoded, not freestyled.

    Satisfies CASL s.6 (identification: business + individual + physical address
    + a second contact method) and s.11 (readily-performed, zero-cost unsubscribe
    with the 10-business-day SLA). This is appended to EVERY draft/email.
    """
    addr = SENDER_ADDRESS or "[SENDER_ADDRESS not set]"
    second_contact = SENDER_PHONE or SENDER_WEBSITE
    return (
        "\n\n--\n"
        f"{SENDER_NAME}\n"
        f"{SENDER_INDIVIDUAL}\n"
        f"{addr}\n"
        f"{second_contact}\n\n"
        "To unsubscribe: reply \"UNSUBSCRIBE\" or email "
        f"{REPLY_TO_EMAIL}. We will remove you within 10 business days."
    )


def has_do_not_contact_statement(text: str) -> bool:
    """PILLAR A guard: detect an explicit refusal to receive unsolicited CEMs.

    If a business's published contact page states they do not wish to receive
    cold pitches, CASL implied-consent (conspicuous publication) is VOID and we
    must not contact them. Returns True if such a statement is found.
    """
    if not text:
        return False
    t = text.lower()
    markers = (
        "no spam", "no cold", "no unsolicited", "do not contact", "don't contact",
        "do not send", "no solicitations", "no pitches", "we do not accept cold",
        "not accept cold", "no marketing emails", "do not email us with",
    )
    return any(m in t for m in markers)


def pre_send_check(lead: dict) -> tuple[bool, str]:
    """CASL/PIPA pre-send guardrail (report §3B). Returns (ok, reason).

    A lead is cleared to send only if ALL hold:
      - has a BUSINESS email (PIPA: no personal/webmail addresses)
      - consent_type is IMPLIED_CONSPICUOUS and a source_url is recorded
        (Pillar A proof of conspicuous publication)
      - not on the DNC list (Pillar C)
      - SENDER_ADDRESS is set (Pillar B identification)
    This is the final gate before any CEM leaves the building.
    """
    to = (lead.get("email") or "").strip()
    if not to:
        return False, "no email on file"
    if not is_business_email(to):
        return False, f"personal/webmail address ({to}) — PIPA violation, refuse"
    if is_blocked(to):
        return False, f"on DNC list ({to})"
    if not SENDER_ADDRESS:
        return False, "SENDER_ADDRESS not set (Pillar B)"
    consent = (lead.get("consent_type") or "").strip().upper()
    src = (lead.get("source_url") or "").strip()
    if consent != "IMPLIED_CONSPICUOUS" or not src:
        return False, "missing consent proof (consent_type/source_url) — Pillar A"
    return True, "ok"


# SMTP host for sending — defaults to Gmail; override for Yandex 360
# (smtp.yandex.com) or another provider. Port is always 465 (SSL).
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.zoho.com")


def send_email(to_email: str, subject: str, body: str) -> str:
    """Send a CEM via Zoho Mail SMTP (pivot from Gmail/Resend). Appends the CASL footer."""
    zoho_user = GMAIL_USER
    zoho_pw = GMAIL_APP_PASSWORD

    if not zoho_user or not zoho_pw:
        return "ERROR: GMAIL_USER / GMAIL_APP_PASSWORD (Zoho creds) not set."
    if not to_email:
        return "ERROR: no recipient email on file."
    if is_blocked(to_email):
        return f"ERROR: {to_email} is on the do-not-contact list (unsubscribed)."
    if not SENDER_ADDRESS:
        return "ERROR: SENDER_ADDRESS not set — required for CASL compliance. Refusing to send."

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = f"Pacific Yew Automations <{zoho_user}>"
    msg['To'] = to_email
    msg['Reply-To'] = zoho_user
    msg.set_content(body + casl_footer())

    try:
        with smtplib.SMTP_SSL('smtp.zoho.com', 465) as smtp:
            smtp.login(zoho_user, zoho_pw)
            smtp.send_message(msg)
        return "SENT"
    except Exception as e:
        print(f"Zoho SMTP error: {e}")
        return f"Error sending: {e}"


def send_approved(limit: int = None) -> int:
    """Send ONLY leads whose status == APPROVED and that have an email. Never touches DRAFT_READY.
    Marks each SENT (+ sent_at) or SEND_ERROR / NEEDS_EMAIL. Returns count sent."""
    limit = limit or SEND_LIMIT
    if not SENDER_ADDRESS:
        print("SENDER_ADDRESS not set — skipping send (CASL). Set it in .env / secrets to enable sending.")
        return 0
    leads = get_leads(limit=10000)
    sent = 0
    for lead in leads:
        if sent >= limit:
            print(f"Reached SEND_LIMIT ({limit}); stopping.")
            break
        if (lead.get("status") or "").strip().upper() != "APPROVED":
            continue
        to = (lead.get("email") or "").strip()
        if not to:
            update_lead(lead["_row"], {"status": "NEEDS_EMAIL"})
            print(f"No email for {lead.get('business_name')} — marked NEEDS_EMAIL.")
            continue
        # ── CASL/PIPA pre-send guardrail (report §3B) ──
        ok, reason = pre_send_check(lead)
        if not ok:
            # Personal email / missing consent proof / blocked → do NOT send.
            new_status = "DO_NOT_CONTACT" if is_blocked(to) else "BLOCKED_COMPLIANCE"
            update_lead(lead["_row"], {"status": new_status})
            print(f"PRE-SEND BLOCKED ({reason}): {to} — marked {new_status}.")
            continue
        subject = lead.get("email_subject") or f"Quick idea for {lead.get('business_name', 'your team')}"
        body = (lead.get("email_body") or lead.get("agent_analysis") or "").strip()
        if len(body) < 20:  # no usable draft — don't send an empty shell
            update_lead(lead["_row"], {"status": "NEEDS_DRAFT"})
            print(f"Empty/short body for {lead.get('business_name')} — marked NEEDS_DRAFT.")
            continue
        result = send_email(to, subject, body)
        if result == "SENT":
            update_lead(lead["_row"], {"status": "SENT", "sent_at": datetime.now(timezone.utc).isoformat()})
            sent += 1
            print(f"Sent to {to} ({lead.get('business_name')})")
            time.sleep(2)  # gentle pacing for Gmail
        else:
            update_lead(lead["_row"], {"status": "SEND_ERROR"})
            print(f"Send failed for {to}: {result}")
    print(f"send_approved: {sent} email(s) sent.")
    return sent


# ─── Search queries (Pacific Yew ICP) ────────────────────────────────────────
CITIES = ["Vancouver", "Surrey", "Burnaby", "Richmond", "Coquitlam", "Langley", "North Vancouver"]
NICHES = [
    "HVAC company", "plumbing company", "electrician", "roofing company",
    "landscaping company", "general contractor", "pest control company",
    "law firm", "personal injury lawyer", "family law firm", "immigration lawyer",
    "physiotherapy clinic", "chiropractor", "dental clinic", "massage therapy clinic",
    "veterinary clinic", "optometrist", "med spa", "real estate brokerage",
    "accounting firm", "insurance broker", "mortgage broker",
]
SEARCH_QUERIES = [f"{n} {c}" for c in CITIES for n in NICHES]
QUERIES_PER_RUN = int(os.environ.get("QUERIES_PER_RUN", "6"))


def queries_for_today():
    n = len(SEARCH_QUERIES)
    start = (datetime.now().timetuple().tm_yday * QUERIES_PER_RUN) % n
    return [SEARCH_QUERIES[(start + i) % n] for i in range(min(QUERIES_PER_RUN, n))]


# ─── Pipeline ────────────────────────────────────────────────────────────────
def discover_and_draft():
    """Discover → filter directories → scrape email → draft subject+body → store.
    New leads are buffered and written in ONE batched Sheets call to avoid the
    append_row race that silently drops rows under rapid inserts."""
    queries = queries_for_today()
    print(f"Today's searches ({len(queries)}): {queries}")
    seen_websites = set()
    buffer = []
    for query in queries:
        print(f"\n── Searching: {query} ──")
        for biz in discover_businesses(query):
            site = biz.get("website")
            if not site or site in seen_websites:
                continue
            seen_websites.add(site)
            if is_directory(biz):
                print(f"Skipping directory: {biz.get('title')}")
                continue
            if dedup_exists(site):
                print(f"Already contacted: {biz.get('title')}")
                continue

            website_text = scrape_website(site)
            email = scrape_email(site)
            draft = analyze_and_draft(biz, website_text)

            data = {
                "business_name": biz.get("title"),
                "website": site,
                "phone": biz.get("phone", ""),
                "email": email,
                "agent_analysis": draft.get("qualified", ""),
                "email_subject": draft.get("subject", ""),
                "email_body": draft.get("body", ""),
                "status": "DRAFT_READY" if email else "NEEDS_EMAIL",
                # ── CASL Pillar A audit trail ──
                # Consent is IMPLIED by conspicuous publication (CASL s.10(9)(b)):
                # the business published its contact email on a public web page we
                # found. Proof = the URL it appeared on, or the business site from
                # discovery when the email wasn't extractable this run. Consent does
                # NOT depend on email extraction — only on a real public source URL.
                # Status (DRAFT_READY vs NEEDS_EMAIL) reflects email availability.
                "source_url": _last_scraped_source_url or site,
                "consent_type": "IMPLIED_CONSPICUOUS" if (_last_scraped_source_url or site) else "",
                "dnc_timestamp": "",
                "dnc_processed": "",
            }
            buffer.append(data)
            print(f"Buffered: {biz.get('title')} | email={email or 'none'} | {data['status']} | src={_last_scraped_source_url or 'n/a'}")
    added = 0
    if buffer:
        try:
            added = insert_leads_batch(buffer)
            print(f"Batch-wrote {added} lead(s) to Sheets.")
        except Exception as e:
            print(f"Sheets batch insert error: {e}")
    print(f"\ndiscover_and_draft: {added} new lead(s) added.")
    return added


def main():
    """Daily run: discover + draft, scan unsubscribes, then send APPROVED leads.
    Nothing is emailed unless a row's status == APPROVED (approve-first, human in the loop).
    A CASL unsubscribe scan runs before sending so opt-outs are honored first."""
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    print(f"Waking up Pacific Yew BDR agent... (mode={mode})")

    if mode in ("all", "scan"):
        print("\n── Scanning for unsubscribe replies ──")
        scan_unsubscribes()
    if mode in ("all", "discover"):
        discover_and_draft()
    if mode in ("all", "send"):
        print("\n── Sending APPROVED leads ──")
        send_approved()


if __name__ == "__main__":
    main()
