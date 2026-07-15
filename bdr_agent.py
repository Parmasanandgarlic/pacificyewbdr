import os
import re
import time
import smtplib
import requests
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

# --- Gmail config (sending approved drafts) ---
GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
REPLY_TO_EMAIL = os.environ.get("REPLY_TO_EMAIL", "contact@pacificyew.pro")

# --- Sender identity (REQUIRED for CASL-compliant sending) ---
SENDER_NAME = os.environ.get("SENDER_NAME", "Pacific Yew Automations")
SENDER_ADDRESS = os.environ.get("SENDER_ADDRESS", "")  # physical mailing address — REQUIRED by CASL
SENDER_WEBSITE = os.environ.get("SENDER_WEBSITE", "https://pacificyew.pro")
SEND_LIMIT = int(os.environ.get("SEND_LIMIT", "20"))  # max emails per run (Gmail-safe)

# Column order for the Leads tab. New columns are APPENDED so existing rows stay aligned.
HEADERS = [
    "business_name", "website", "phone", "email", "agent_analysis",
    "status", "created_at", "email_subject", "email_body", "sent_at",
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
    system_prompt = f"You are a fractional COO and BDR for Pacific Yew (pacificyew.pro).\n{PACIFIC_YEW_ICP}"
    user_prompt = f"""
Business: {business.get('title')}
Website: {business.get('website')}
Website Text (excerpt): {website_text[:3000]}

Write a cold outreach email. Return EXACTLY this format, nothing else:

QUALIFIED: <Yes or No, plus a 1-line reason>
SUBJECT: <compelling subject line, under 60 characters, no clickbait>
BODY:
<a 4-sentence cold email. Sentence 1: a specific hook about THEIR business from the website.
Sentence 2: we build internal AI automation / data graph for relationship-based service businesses.
Sentence 3: we are not a marketing agency — we build the internal system.
Sentence 4: ask for a 15-minute discovery call.
Do NOT include a signature, greeting name you don't know, or any footer.>
"""
    out = _or_chat(system_prompt, user_prompt) or ""
    qualified = _extract(out, r"QUALIFIED:\s*(.+?)(?:\n|$)")
    subject = _extract(out, r"SUBJECT:\s*(.+?)(?:\n|$)")
    body_m = re.search(r"BODY:\s*(.+)$", out, re.S | re.I)
    body = body_m.group(1).strip() if body_m else ""
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
def scrape_website(website_url):
    try:
        if not website_url.startswith("http"):
            website_url = "https://" + website_url
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


def scrape_email(website_url) -> str:
    """Fetch homepage + common contact pages, extract the best business email. Returns '' if none."""
    if not website_url:
        return ""
    if not website_url.startswith("http"):
        website_url = "https://" + website_url
    base = website_url.rstrip("/")
    candidates = []
    for path in ("", "/contact", "/contact-us", "/about", "/about-us"):
        try:
            r = requests.get(base + path, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
            for e in EMAIL_RE.findall(r.text):
                el = e.lower().strip(".")
                if any(j in el for j in JUNK_EMAIL_HINTS):
                    continue
                candidates.append(el)
        except Exception:
            continue
        if candidates:
            break  # stop at the first page that yields an email
    if not candidates:
        return ""
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
        if _sheet.col_count < len(HEADERS):
            _sheet.add_cols(len(HEADERS) - _sheet.col_count)
        existing = _sheet.row_values(1)
        if not existing:
            _sheet.append_row(HEADERS)
        elif existing != HEADERS:
            _sheet.update("A1", [HEADERS])  # extend header, existing data columns stay put
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
    data = dict(data)
    data.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    row = [data.get(h, "") for h in HEADERS]
    get_sheet().append_row(row)


def update_lead(row_id: int, fields: dict):
    ws = get_sheet()
    header = ws.row_values(1)
    for k, v in fields.items():
        if k in header:
            ws.update_cell(row_id, header.index(k) + 1, v)


# ─── Gmail (sending) ────────────────────────────────────────────────────────
def casl_footer() -> str:
    """CASL-compliant footer: identity + physical address + unsubscribe."""
    addr = SENDER_ADDRESS or "[SENDER_ADDRESS not set]"
    return (
        "\n\n--\n"
        f"{SENDER_NAME}\n"
        f"{addr}\n"
        f"{SENDER_WEBSITE}\n\n"
        "You received this one-time message because your business may benefit from automation. "
        "To stop receiving emails, reply with 'UNSUBSCRIBE' and we will remove you within 10 business days."
    )


def send_email(to_addr: str, subject: str, body: str) -> str:
    """Send an email via Gmail SMTP. Appends the CASL footer. Returns a status string."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        return "ERROR: GMAIL_USER / GMAIL_APP_PASSWORD not set."
    if not to_addr:
        return "ERROR: no recipient email on file."
    if not SENDER_ADDRESS:
        return "ERROR: SENDER_ADDRESS not set — required for CASL compliance. Refusing to send."
    msg = EmailMessage()
    msg["From"] = f"{SENDER_NAME} <{GMAIL_USER}>"
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Reply-To"] = REPLY_TO_EMAIL
    msg.set_content(body + casl_footer())
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.send_message(msg)
        return "SENT"
    except Exception as e:
        return f"ERROR: {e}"


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
        subject = lead.get("email_subject") or f"Quick idea for {lead.get('business_name', 'your team')}"
        body = lead.get("email_body") or lead.get("agent_analysis", "")
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
    """Discover → filter directories → scrape email → draft subject+body → store."""
    queries = queries_for_today()
    print(f"Today's searches ({len(queries)}): {queries}")
    seen_websites = set()
    added = 0
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
            }
            try:
                insert_lead(data)
                added += 1
                print(f"Inserted: {biz.get('title')} | email={email or 'none'} | {data['status']}")
            except Exception as e:
                print(f"Sheets insert error for {biz.get('title')}: {e}")
    print(f"\ndiscover_and_draft: {added} new lead(s) added.")
    return added


def main():
    """Daily run: discover + draft, then send any leads YOU marked APPROVED.
    Nothing is emailed unless a row's status == APPROVED (approve-first, human in the loop)."""
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    print(f"Waking up Pacific Yew BDR agent... (mode={mode})")

    if mode in ("all", "discover"):
        discover_and_draft()
    if mode in ("all", "send"):
        print("\n── Sending APPROVED leads ──")
        send_approved()


if __name__ == "__main__":
    main()
