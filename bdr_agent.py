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
# openrouter/free auto-routes to the best available free endpoint, handles rate
# limits gracefully, and always returns OpenAI-compatible response format.
# Previously: tencent/hy3:free (died 2026-07), google/gemma-4-26b-a4b-it:free
# (inconsistent response format — sometimes missing 'choices').
DEFAULT_MODEL = "openrouter/free"

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
# Effective reply/unsub address: explicit REPLY_TO_EMAIL if set (incl. fallback to
# sender), else derive from GMAIL_USER. NOTE: os.environ.get returns the default ONLY
# when the var is ABSENT — an empty-string secret would otherwise stay falsy and break
# the pre-flight. .strip()+or-chain treats empty/missing identically.
REPLY_TO_EMAIL = (os.environ.get("REPLY_TO_EMAIL") or GMAIL_USER or "contact@pacificyew.pro").strip()

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
            if len(results) >= RESULTS_PER_QUERY:
                break
        print(f"Discovered {len(results)} businesses via web search.")
        return results
    except Exception as e:
        print(f"Discovery error: {e}")
        return []


def _discover_apify(search_query: str, actor: str):
    token = os.environ.get("APIFY_TOKEN")
    api_url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token={token}"
    payload = {"searchStrings": [search_query], "maxCrawledPlacesPerSearch": RESULTS_PER_QUERY}
    try:
        response = requests.post(api_url, json=payload, timeout=120)
        if response.status_code == 200:
            return response.json()
        print(f"Apify error: {response.status_code} - {response.text[:200]}")
    except Exception as e:
        print(f"Apify request failed: {e}")
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
        creds_path = SHEET_CREDS
        if not os.path.exists(creds_path):
            raise RuntimeError(
                f"Google service-account creds not found at '{creds_path}'. "
                f"Set GOOGLE_SHEET_CREDS or place the file. Cannot open the Leads sheet."
            )
        try:
            creds = Credentials.from_service_account_file(creds_path, scopes=SHEET_SCOPES)
        except Exception as e:
            raise RuntimeError(f"Failed to load Google creds from '{creds_path}': {e}")
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


def _norm_url(u: str) -> str:
    """Strip scheme/www/trailing slash + lowercase so http://www.x.com/ and
    https://x.com match as the same site (the bug that doubled the Sheet)."""
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")


def already_known(website: str = "", email: str = "", name: str = "") -> bool:
    """True if this business is already in the contacted universe (SENT/DNC/
    BLOCKED), matched on normalized website, email, or name. Prevents the
    discovery pipeline from re-buffering an existing lead under a URL variant
    (http vs https, www, trailing slash) — the exact failure that duplicated
    the Sheet on the 2026-07-18 run."""
    u = _contacted_universe()
    if email and email.strip().lower() in u["emails"]:
        return True
    if website and _norm_url(website) in u["websites"]:
        return True
    if name and _normalize_name(name) in u["names"]:
        return True
    return False


def dedup_exists(website: str) -> bool:
    return already_known(website=website)


def _normalize_name(name: str) -> str:
    """Aggressive normalization so 'Avanti Mechanical HVAC' and
    'Avanti Mechanical HVAC Ltd.' match as the same business."""
    import re as _re
    n = (name or "").lower()
    n = _re.sub(r"[^a-z0-9 ]", " ", n)
    # drop legal suffixes / filler words
    drop = ("inc", "incorporated", "ltd", "ltda", "llc", "corp", "corporation", "co",
            "company", "companies", "the", "and", "services", "service", "group", "gmbh",
            "pacific", "canada", "bc", "british", "columbia", "vancouver", "surrey",
            "langley", "abbotsford", "richmond", "burnaby", "coquitlam", "north")
    n = " ".join(w for w in n.split() if w not in drop)
    return " ".join(n.split())


def _contacted_universe() -> dict:
    """Single source of truth for 'we have already dealt with this business'.

    Returns {emails, websites, names} covering EVERY contact outcome that must
    never be re-mailed or re-discovered:
      - SENT leads (already emailed)
      - DNC tab (unsubscribed / opted out)
      - BLOCKED_COMPLIANCE / DO_NOT_CONTACT / SENT_DUPLICATE_SKIPPED statuses
    Keyed by email, website, AND normalized business name so a business can't
    slip back in under a new domain, a www/non-www variant, or a slightly
    different name. Cached per run.
    """
    global _CONTACTED_CACHE
    if _CONTACTED_CACHE is not None:
        return _CONTACTED_CACHE
    emails, websites, names = set(), set(), set()
    try:
        ws = get_sheet()
        vals = ws.get_all_values()
        if len(vals) >= 2:
            hdr = vals[0]
            ecol = hdr.index("email") if "email" in hdr else None
            wcol = hdr.index("website") if "website" in hdr else None
            ncol = hdr.index("business_name") if "business_name" in hdr else None
            scol = hdr.index("status") if "status" in hdr else None
            sentset = {"SENT", "BLOCKED_COMPLIANCE", "DO_NOT_CONTACT",
                       "SENT_DUPLICATE_SKIPPED"}
            for r in vals[1:]:
                st = r[scol].strip().upper() if (scol is not None and len(r) > scol) else ""
                # Only TRULY-CONTACTED rows belong in the no-resend universe:
                # SENT (emailed), BLOCKED_COMPLIANCE / DO_NOT_CONTACT (refused or
                # opted out), SENT_DUPLICATE_SKIPPED (guard hit). DRAFT_READY and
                # NEEDS_EMAIL are FRESH, uncontacted leads — they must stay
                # sendable and re-discoverable, so they are deliberately excluded.
                if st not in sentset:
                    continue
                if ecol is not None and len(r) > ecol and r[ecol].strip():
                    emails.add(r[ecol].strip().lower())
                if wcol is not None and len(r) > wcol and r[wcol].strip():
                    websites.add(r[wcol].strip().lower())
                if ncol is not None and len(r) > ncol and r[ncol].strip():
                    names.add(_normalize_name(r[ncol]))
    except Exception as e:
        print(f"[universe] Could not read Leads tab ({e}); universe empty.")
    # Fold in the DNC tab (opt-outs) — emails only.
    try:
        for e in _blocked_set():
            emails.add(e)
    except Exception:
        pass
    # Fold in the immutable Sent Ledger (authoritative: survives crashes).
    try:
        ledger_emails = _load_ledger_emails()
        emails.update(ledger_emails)
    except Exception:
        pass
    _CONTACTED_CACHE = {"emails": emails, "websites": websites, "names": names}
    return _CONTACTED_CACHE


_CONTACTED_CACHE = None



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
    """Append lead rows TRUSTWORTHILY, with column-order stability.

    Every row — existing AND new — is emitted strictly in canonical HEADERS
    order via keyed dicts. This makes column shifts impossible: the on-disk
    position of a field never depends on the sheet's current header order.
    The previous version rebuilt the table positionally, which shifted values
    whenever _reconcile_header reordered/added columns.
    """
    if not rows:
        return 0
    ws = get_sheet()
    header = _reconcile_header(ws)  # current sheet header (may differ in order)
    now = datetime.now(timezone.utc).isoformat()

    # New rows → keyed dicts in canonical HEADERS order.
    new_dicts = []
    for data in rows:
        d = {k: (data.get(k, "") or "") for k in HEADERS}
        d["created_at"] = data.get("created_at") or now
        new_dicts.append(d)

    # Belt-and-suspenders: never append a row whose normalized email/site/name
    # already exists in the live Sheet. This is what structurally prevents the
    # 2026-07-18 doubling (a discovered lead re-buffered as "new" because its
    # URL differed by scheme/www/trailing slash from the stored one).
    existing = ws.get_all_values()
    existing_data = existing[1:] if len(existing) > 1 else []
    existing_keys = set()
    for r in existing_data:
        rd = dict(zip(header, list(r) + [""] * (len(header) - len(r))))
        e = (rd.get("email") or "").strip().lower()
        w = _norm_url(rd.get("website") or "")
        n = _normalize_name(rd.get("business_name") or "")
        if e:
            existing_keys.add(("email", e))
        if w:
            existing_keys.add(("site", w))
        if n:
            existing_keys.add(("name", n))
    before = len(new_dicts)
    new_dicts = [d for d in new_dicts if not (
        (d["email"].strip().lower() and ("email", d["email"].strip().lower()) in existing_keys) or
        (d["website"] and ("site", _norm_url(d["website"])) in existing_keys) or
        (d["business_name"] and ("name", _normalize_name(d["business_name"])) in existing_keys)
    )]
    dropped = before - len(new_dicts)
    if dropped:
        print(f"[dedup] skipped {dropped} row(s) already present in Sheet.")

    # Existing rows → keyed dicts via CURRENT header, then re-emitted in HEADERS
    # order. Corruption already present in existing cells is preserved byte-for-
    # byte here; the separate repair step is what cleans it. This function is
    # only about NOT INTRODUCING new shifts.
    existing = ws.get_all_values()
    existing_data = existing[1:] if len(existing) > 1 else []
    existing_dicts = []
    for r in existing_data:
        rd = {header[i]: (r[i] if i < len(r) else "") for i in range(len(header))}
        existing_dicts.append(rd)

    all_dicts = existing_dicts + new_dicts
    new_table = [[d.get(h, "") for h in HEADERS] for d in all_dicts]
    new_table = [list(HEADERS)] + new_table

    target = len(new_table)
    ws.update("A1", new_table, value_input_option="USER_ENTERED")
    # Verify what actually persisted (read back).
    persisted = 0
    for _ in range(3):
        persisted = len(ws.get_all_values())
        if persisted >= target:
            break
        time.sleep(1.5)
    actual_new = persisted - len(existing)
    # Sheet changed — drop the contacted-universe cache so the next discovery
    # pass (or send) sees the freshly inserted leads.
    global _CONTACTED_CACHE
    _CONTACTED_CACHE = None
    return max(0, actual_new)


def update_lead(row_id: int, fields: dict):
    ws = get_sheet()
    header = ws.row_values(1)
    # Build Cell objects for batch update to avoid 429 quota crashes.
    cells = []
    for k, v in fields.items():
        if k in header:
            cells.append(gspread.Cell(row=row_id, col=header.index(k) + 1, value=v))
    if cells:
        # Retry once on 429 (Google Sheets write quota limit ~60/min).
        for attempt in range(2):
            try:
                ws.update_cells(cells)
                break
            except gspread.exceptions.APIError as e:
                if "429" in str(e) and attempt == 0:
                    print(f"[update_lead] 429 on row {row_id}, sleeping 3s and retrying...")
                    time.sleep(3)
                    continue
                raise
    # Sheet changed — drop the contacted-universe cache so subsequent reads
    # (e.g. the next lead in this same send loop) see the latest state.
    global _CONTACTED_CACHE
    _CONTACTED_CACHE = None


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


# ─── Immutable Sent Ledger (authoritative contacted record) ───────────────────
# This tab is append-ONLY. Once a row is written it is never modified or
# deleted. Checked BEFORE every SMTP call in send_approved. Written AFTER
# SMTP success but BEFORE the lead status is updated — so a crash between
# ledger write and status update still prevents duplicate sends on retry.
LEDGER_TAB = "Sent Ledger"
_LEDGER_CACHE = None


def _ensure_ledger():
    """Create the Sent Ledger tab if missing. Return the worksheet."""
    creds = Credentials.from_service_account_file(SHEET_CREDS, scopes=SHEET_SCOPES)
    client = gspread.authorize(creds)
    sh = client.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(LEDGER_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=LEDGER_TAB, rows=1000, cols=5)
        ws.append_row(["email", "business_name", "sent_at", "subject", "source"])
    # Ensure header exists
    if not ws.row_values(1):
        ws.append_row(["email", "business_name", "sent_at", "subject", "source"])
    return ws


def _load_ledger_emails() -> set:
    """Return ALL normalized email addresses ever recorded in the Sent Ledger.
    Cached per run via _LEDGER_CACHE."""
    global _LEDGER_CACHE
    if _LEDGER_CACHE is not None:
        return _LEDGER_CACHE
    try:
        ws = _ensure_ledger()
        rows = ws.get_all_values()
        if len(rows) < 2:
            _LEDGER_CACHE = set()
            return _LEDGER_CACHE
        hdr = rows[0]
        ecol = hdr.index("email") if "email" in hdr else None
        if ecol is None:
            _LEDGER_CACHE = set()
            return _LEDGER_CACHE
        _LEDGER_CACHE = {r[ecol].strip().lower() for r in rows[1:]
                         if len(r) > ecol and r[ecol].strip()
                         and "@" in r[ecol].strip()}
        return _LEDGER_CACHE
    except Exception as e:
        print(f"[ledger] Could not read Sent Ledger ({e}); treating as empty.")
        _LEDGER_CACHE = set()
        return _LEDGER_CACHE


def _in_sent_ledger(email: str) -> bool:
    """True if this normalized email has EVER been successfully sent."""
    if not email or "@" not in email:
        return False
    return email.strip().lower() in _load_ledger_emails()


def _append_to_ledger(email: str, business_name: str, subject: str,
                      source: str = "bdr_agent"):
    """Append one row to the immutable Sent Ledger. Invalidates caches."""
    try:
        ws = _ensure_ledger()
        ws.append_row([
            email.strip().lower(),
            (business_name or "").strip(),
            datetime.now(timezone.utc).isoformat(),
            (subject or "").strip(),
            source,
        ])
        # Invalidate both ledger cache and contacted-universe cache so
        # subsequent reads in the same run pick up the new entry.
        global _LEDGER_CACHE, _CONTACTED_CACHE
        _LEDGER_CACHE = None
        _CONTACTED_CACHE = None
    except Exception as e:
        print(f"[ledger] ERROR appending to Sent Ledger: {e}")
        # DO NOT raise — a ledger write failure must NOT block the send.
        # The lead's status is still being updated and the contacted
        # cache will include this SENT status on the next check. The
        # worst case is a duplicate on a future run if BOTH this ledger
        # write AND the lead status update fail, but that's a last-resort
        # edge case. The hard guard in send_approved (step 1) handles the
        # common case.


def _invalidate_ledger():
    """Drop the per-run ledger cache (called after sheet-wide writes)."""
    global _LEDGER_CACHE
    _LEDGER_CACHE = None


def _blocked_to_send() -> dict:
    """Universe of leads that must NEVER be emailed again.

    Union of SENT + DNC + BLOCKED statuses, keyed by email, website, and
    normalized business name. A hard guard: once a business is in here, no
    run — scheduled or manual, however approved — can re-mail it. This is the
    structural fix for repeat sends."""
    return _contacted_universe()


def send_approved(limit: int = None) -> int:
    """Send ONLY leads whose status == APPROVED. Uses an append-only Sent Ledger
    as the authoritative contacted record (checked BEFORE every SMTP call).
    Each lead is RESERVED before SMTP, then ledger-written + status-updated
    after success — so a crash after SMTP but before status update still
    prevents re-sends on the next run.

    Returns count sent. Never touches RESERVED / SENT / DRAFT_READY."""
    limit = limit or SEND_LIMIT
    if not SENDER_ADDRESS:
        print("SENDER_ADDRESS not set — skipping send (CASL). Set it in .env / secrets to enable sending.")
        return 0
    universe = _blocked_to_send()
    sent_emails, sent_sites, sent_names = universe["emails"], universe["websites"], universe["names"]
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

        # ── GUARD 1: Immutable Sent Ledger (survives crashes) ──
        # Checked BEFORE any other guard because the ledger is the single
        # source of truth for what actually left the SMTP server.
        if _in_sent_ledger(to):
            update_lead(lead["_row"], {"status": "SENT_LEDGER_DEDUP"})
            print(f"SKIP (in Sent Ledger): {to} — marked SENT_LEDGER_DEDUP.")
            continue

        # ── GUARD 2: Contacted universe (SENT/DNC/BLOCKED statuses) ──
        site_key = (lead.get("website") or "").strip().lower()
        name_key = _normalize_name(lead.get("business_name") or "")
        if (to.lower() in sent_emails
                or (site_key and site_key in sent_sites)
                or (name_key and name_key in sent_names)):
            update_lead(lead["_row"], {"status": "SENT_DUPLICATE_SKIPPED"})
            print(f"SKIP repeat send (already contacted): {to} — marked SENT_DUPLICATE_SKIPPED.")
            continue

        # ── GUARD 3: CASL/PIPA pre-send guardrail ──
        ok, reason = pre_send_check(lead)
        if not ok:
            new_status = "DO_NOT_CONTACT" if is_blocked(to) else "BLOCKED_COMPLIANCE"
            update_lead(lead["_row"], {"status": new_status})
            print(f"PRE-SEND BLOCKED ({reason}): {to} — marked {new_status}.")
            continue

        # ── RESERVED: claim this lead before SMTP ──
        # If the run crashes after this point, a future run will find the
        # lead in RESERVED status and skip it (send_approved only picks
        # APPROVED). The Sent Ledger check (Guard 1) is the structural
        # backstop: even if someone manually sets the lead back to APPROVED,
        # the ledger will reject the duplicate.
        update_lead(lead["_row"], {"status": "RESERVED"})

        subject = lead.get("email_subject") or f"Quick idea for {lead.get('business_name', 'your team')}"
        body = (lead.get("email_body") or lead.get("agent_analysis") or "").strip()
        if len(body) < 20:
            update_lead(lead["_row"], {"status": "NEEDS_DRAFT"})
            print(f"Empty/short body for {lead.get('business_name')} — marked NEEDS_DRAFT.")
            continue

        result = send_email(to, subject, body)
        if result == "SENT":
            # Write ledger BEFORE status update — this is the authoritative
            # record that the email left the server. A crash after this
            # append but before the status update is SAFE: the next run
            # checks the ledger (Guard 1) and skips this recipient.
            biz_name = lead.get("business_name") or ""
            _append_to_ledger(to, biz_name, subject)
            update_lead(lead["_row"], {"status": "SENT", "sent_at": datetime.now(timezone.utc).isoformat()})
            sent += 1
            print(f"Sent to {to} ({biz_name})")
            time.sleep(2)  # gentle pacing for SMTP
        else:
            update_lead(lead["_row"], {"status": "SEND_ERROR"})
            print(f"Send failed for {to}: {result}")
    print(f"send_approved: {sent} email(s) sent.")
    return sent


# ─── Search queries (Pacific Yew ICP) ────────────────────────────────────────
# Lower Mainland + Fraser Valley + Greater Vancouver coverage.
CITIES = [
    "Vancouver", "Surrey", "Burnaby", "Richmond", "Coquitlam", "Langley",
    "North Vancouver", "Maple Ridge", "Delta", "White Rock",
    "Port Coquitlam", "New Westminster", "Pitt Meadows",
    "West Vancouver", "Port Moody", "Tsawwassen",
]
# Service trades, clinics, and professional-services firms we can help.
NICHES = [
    # Trades
    "HVAC company", "furnace repair", "air conditioning company", "plumbing company",
    "drain cleaning", "electrician", "roofing company", "roof repair",
    "landscaping company", "lawn care", "general contractor", "renovation company",
    "kitchen renovation", "bathroom renovation", "painting company", "flooring company",
    "concrete company", "excavation company", "fencing company", "deck builder",
    "solar panel company", "window cleaning", "gutter cleaning", "junk removal",
    "pest control company", "pressure washing", "snow removal", "property management",
    "handyman", "carpet cleaning", "moving company", "appliance repair",
    # Automotive
    "auto repair", "mechanic", "auto body shop", "tire shop", "car detailing",
    # Cleaning / home
    "house cleaning", "commercial cleaning", "maid service", "cleaning company",
    # Personal services
    "hair salon", "barber shop", "nail salon", "med spa", "esthetician",
    "tattoo studio", "pet grooming", "dog daycare", "daycare",
    # Health clinics
    "physiotherapy clinic", "chiropractor", "dental clinic", "massage therapy clinic",
    "RMT clinic", "veterinary clinic", "optometrist", "dentist", "orthodontist",
    "podiatrist", "hearing clinic", "speech therapy", "counseling", "therapy clinic",
    "dermatology clinic", "walk-in clinic", "senior care", "home care",
    # Legal / finance / professional
    "law firm", "personal injury lawyer", "family law firm", "immigration lawyer",
    "real estate lawyer", "notary public", "paralegal", "accounting firm",
    "bookkeeping", "tax preparation", "insurance broker", "mortgage broker",
    "financial advisor", "real estate brokerage", "insurance agency",
    # Other local pro services
    "photographer", "marketing agency", "web design", "IT support", "managed service provider",
    "recruiter", "fitness studio", "gym", "yoga studio",
]
SEARCH_QUERIES = [f"{n} {c}" for c in CITIES for n in NICHES]
QUERIES_PER_RUN = int(os.environ.get("QUERIES_PER_RUN", "12"))
RESULTS_PER_QUERY = int(os.environ.get("RESULTS_PER_QUERY", "8"))  # businesses pulled per search


def queries_for_today():
    """Queries for this run. Normally a rotating window of QUERIES_PER_RUN
    (spreads coverage across the full query list over many days). If BACKFILL=1
    is set (manual large sweep), returns EVERY query at once for a full scan."""
    n = len(SEARCH_QUERIES)
    if os.environ.get("BACKFILL", "").strip() == "1":
        print("BACKFILL mode: scanning ALL query combinations.")
        return list(SEARCH_QUERIES)
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


def auto_approve_qualified() -> int:
    """Flip legality-clear DRAFT_READY leads to APPROVED so the daily run can
    send without a human in the loop. Gated HARD by the same bar as manual
    approval (per Michael's standing rule: 'we only unblock if they meet our
    requirements for legality'):
      - status == DRAFT_READY
      - business-domain email (PIPA: no personal/webmail)
      - consent_type == IMPLIED_CONSPICUOUS AND a real http(s) source_url
      - not on DNC / not blocked
    Returns count flipped. Never touches SENT / BLOCKED / NEEDS_EMAIL / already
    APPROVED. pre_send_check runs AGAIN at send time as a second gate.

    Uses batch update_cells (single API call) instead of row-by-row update_cell
    to avoid Google Sheets 60-writes-per-minute quota."""

    approved = 0
    try:
        ws = get_sheet()
        vals = ws.get_all_values()
        if len(vals) < 2 or "email" not in vals[0]:
            return 0
        hdrs = vals[0]
        e_i, st_i, src_i, con_i = (hdrs.index("email"), hdrs.index("status"),
                                    hdrs.index("source_url"), hdrs.index("consent_type"))
        to_approve = []
        for i, r in enumerate(vals[1:], start=2):
            if len(r) <= st_i:
                continue
            if (r[st_i] or "").strip().upper() != "DRAFT_READY":
                continue
            email = (r[e_i] if len(r) > e_i else "").strip()
            consent = (r[con_i] if len(r) > con_i else "").strip().upper()
            src = (r[src_i] if len(r) > src_i else "").strip()
            if not is_business_email(email):
                continue
            if consent != "IMPLIED_CONSPICUOUS" or not src.startswith("http"):
                continue
            if is_blocked(email):
                continue
            # Skip if already in the immutable Sent Ledger (already sent,
            # even if status got reset).
            if _in_sent_ledger(email):
                continue
            to_approve.append((i, email))

        if to_approve:
            # Batch ALL status updates in ONE API call — avoids 429 quota crash.
            cells = [gspread.Cell(row=row, col=st_i + 1, value="APPROVED")
                     for row, _ in to_approve]
            ws.update_cells(cells)
            for row, email in to_approve:
                approved += 1
                print(f"[auto-approve] {email} -> APPROVED (legality-clear).")
    except Exception as e:
        print(f"[auto-approve] error: {e}")
    print(f"auto_approve_qualified: {approved} lead(s) flipped to APPROVED.")
    return approved


def preflight_checks() -> bool:
    """Pre-send readiness gate. Returns True only if the run can actually send.
    Fails LOUD (prints + returns False) so a 'success' run can't silently send 0."""
    ok = True
    # 1) Send identity present (CASL Pillar B). Use the EFFECTIVE reply address
    # (explicit REPLY_TO_EMAIL, else GMAIL_USER) so an unset/empty secret can't
    # abort a perfectly sendable run.
    if not SENDER_ADDRESS:
        print("[PREFLIGHT] FAIL: SENDER_ADDRESS not set (CASL)."); ok = False
    if not (REPLY_TO_EMAIL or GMAIL_USER):
        print("[PREFLIGHT] FAIL: no reply/unsub address (REPLY_TO_EMAIL + GMAIL_USER both empty)."); ok = False
    # 2) SMTP auth reachable (Zoho) — fail loud if creds expired
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, 465, timeout=15) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        print("[PREFLIGHT] SMTP auth OK.")
    except Exception as e:
        print(f"[PREFLIGHT] FAIL: SMTP auth ({SMTP_HOST}): {e}"); ok = False
    # 3) IMAP reachable (unsub scan depends on it)
    try:
        import imaplib
        with imaplib.IMAP4_SSL("imap.zoho.com", 993, timeout=15) as m:
            m.login(GMAIL_USER, GMAIL_APP_PASSWORD); m.select("INBOX")
        print("[PREFLIGHT] IMAP auth OK.")
    except Exception as e:
        print(f"[PREFLIGHT] FAIL: IMAP auth (imap.zoho.com): {e}"); ok = False
    # 4) Sheet reachable + header intact
    try:
        ws = get_sheet(); rows = ws.get_all_values()
        if rows and rows[0] == HEADERS:
            print(f"[PREFLIGHT] Sheet OK ({len(rows)-1} data rows).")
        else:
            print("[PREFLIGHT] FAIL: Sheet header mismatch/corrupt."); ok = False
    except Exception as e:
        print(f"[PREFLIGHT] FAIL: Sheet unreachable: {e}"); ok = False
    print(f"[PREFLIGHT] {'ALL GREEN' if ok else 'FAILED — aborting send.'}")
    return ok


def main():
    """Daily run: discover + draft, scan unsubscribes, then send APPROVED leads.
    Nothing is emailed unless a row's status == APPROVED (approve-first, human in the loop).
    A CASL unsubscribe scan runs before sending so opt-outs are honored first.
    Auto-approve flips legality-clear DRAFT_READY leads so the run actually sends;
    a pre-flight gate fails loud if the run CAN'T deliver (so 'success' can't hide 0 sent)."""
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    print(f"Waking up Pacific Yew BDR agent... (mode={mode})")

    if mode in ("all", "scan"):
        print("\n── Scanning for unsubscribe replies ──")
        scan_unsubscribes()
    if mode in ("all", "discover"):
        discover_and_draft()
    if mode in ("all", "send"):
        # Pre-flight: refuse to report 'success' if we can't actually deliver.
        if not preflight_checks():
            print("\n[ABORT] Pre-flight failed — not sending. Fix creds/Sheet and re-run.")
            return
        # Auto-approve legality-clear DRAFT_READY leads so the run sends.
        auto_approve_qualified()
        print("\n── Sending APPROVED leads ──")
        sent = send_approved()
        # Fail loud: if we had APPROVED rows but sent 0, that's a real problem.
        approved_count = sum(1 for r in get_leads(limit=10000)
                             if (r.get("status") or "").strip().upper() == "APPROVED")
        if approved_count > 0 and sent == 0:
            print(f"\n[ALERT] {approved_count} lead(s) APPROVED but 0 sent — investigate send path.")


if __name__ == "__main__":
    main()
