import os
import re
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
Target: Vancouver/Surrey high-end B2B firms (wealth advisors, luxury real estate, clinics).
Pain Point: Drowning in manual admin, using basic CRMs (or none), missing follow-ups.
Our Solution: "Relationship Intelligence OS" & AI automation (We don't build Zapier wrappers; we build the underlying data graph).
Tone: Quiet Operator. Professional, direct, zero hype.
"""

# --- Google Sheets config (storage) ---
SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", "Pacific Yew Outbound CRM")
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "1yOvQ4kLabFwZaD2RRlPcTzRJbz2ZakHipbZsADqzI1k")
SHEET_CREDS = os.environ.get("GOOGLE_SHEET_CREDS", "creds/service_account.json")
SHEET_TAB = os.environ.get("GOOGLE_SHEET_TAB", "Leads")
SHEET_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",  # needed by gspread.open(name) to locate the file
]

# --- Gmail config (sending approved drafts) ---
# The bot sends VIA a free Gmail relay account; replies go to your real domain inbox.
GMAIL_USER = os.environ.get("GMAIL_USER")            # relay, e.g. pacificyew.bdr@gmail.com
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")  # 16-char app password
REPLY_TO_EMAIL = os.environ.get("REPLY_TO_EMAIL", "contact@pacificyew.pro")  # your real inbox

# Column order for the Leads tab
HEADERS = ["business_name", "website", "phone", "email", "agent_analysis", "status", "created_at"]


# ─── OpenRouter (drafting) ────────────────────────────────────────────────────
def _get_openrouter():
    global _or_key, _or_model
    if _or_key is None:
        _or_key = os.environ.get("OPENROUTER_API_KEY")
        _or_model = os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)
    return _or_key, _or_model


def qualify_and_draft(business):
    """Uses OpenRouter to qualify the lead and draft the email."""
    api_key, model = _get_openrouter()
    if not api_key:
        return "Error: OPENROUTER_API_KEY is not set. Add it to .env."

    website_text = scrape_website(business.get("website", ""))

    user_prompt = f"""
    Business Data: {business}
    Website Text: {website_text}

    Task 1: Qualify. Based on the data, is this a good fit for a $10k-$15k Internal OS package? (Yes/No and why).
    Task 2: Draft. If yes, write a 4-sentence cold email to {business.get('title')}.
    - Hook: Mention something specific about their firm from the website.
    - Credibility: Mention we build event-driven architecture (MCP, Supabase) for relationship-based businesses.
    - Differentiation: We aren't a marketing agency; we build the internal data graph.
    - CTA: Ask for a 15-min discovery call.
    """

    system_prompt = f"""You are a fractional COO and BDR for Pacific Yew (pacificyew.pro).
    {PACIFIC_YEW_ICP}"""

    last_err = None
    for attempt in range(4):  # 1 initial + 3 retries with backoff
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
                    "temperature": 0.7,
                },
                timeout=90,
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except Exception as e:
            last_err = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            # Retry on rate limits / transient 5xx; stop on hard 4xx
            if status in (429, 500, 502, 503, 504):
                wait = 2 ** attempt + 1  # 2, 3, 5 seconds
                print(f"OpenRouter transient error ({status}); retrying in {wait}s...")
                import time
                time.sleep(wait)
                continue
            print(f"OpenRouter API error: {e}")
            break
    return f"Error generating draft: {last_err}"


# ─── Discovery (free, key-less web search) ──────────────────────────────────
def discover_businesses(search_query: str = "wealth management firm Vancouver"):
    """Find local businesses via a free DuckDuckGo HTML search (no API key, $0).
    Falls back to Apify's Google Maps scraper ONLY if APIFY_ACTOR is set.
    Returns list of dicts with keys: title, website, phone, email."""
    # Optional Apify path (only if you have a usable actor id configured)
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
        # Parse result titles + links
        import re as _re
        results = []
        # each result block: class="result__a" anchor with href
        blocks = _re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', resp.text, _re.S)
        seen = set()
        for href, title_html in blocks:
            title = _re.sub(r"<[^>]+>", "", title_html).strip()
            # DDG wraps real URL in uddg= param
            m = _re.search(r"uddg=([^&]+)", href)
            site = requests.utils.unquote(m.group(1)) if m else href
            if not site.startswith("http"):
                continue
            domain = _re.sub(r"^https?://(www\.)?", "", site).split("/")[0]
            if domain in seen or any(x in domain for x in ("duckduckgo", "wikipedia", "youtube", "facebook", "instagram", "linkedin")):
                continue
            seen.add(domain)
            results.append({
                "title": title or domain,
                "website": site,
                "phone": "",
                "email": "",
            })
            if len(results) >= 5:
                break
        print(f"Discovered {len(results)} businesses via web search.")
        return results
    except Exception as e:
        print(f"Discovery error: {e}")
        return []


def _discover_apify(search_query: str, actor: str):
    """Optional: only used if APIFY_ACTOR (a resolvable actor id) is configured."""
    token = os.environ.get("APIFY_TOKEN")
    api_url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token={token}"
    payload = {"searchStrings": [search_query], "maxCrawledPlacesPerSearch": 5}
    response = requests.post(api_url, json=payload, timeout=120)
    if response.status_code == 200:
        return response.json()
    print(f"Apify error: {response.status_code} - {response.text}")
    return []


def scrape_website(website_url):
    """Quickly pull text from the homepage to check their tech stack"""
    try:
        if not website_url.startswith("http"):
            website_url = "https://" + website_url

        response = requests.get(website_url, timeout=5)
        text = re.sub(r"<[^>]+>", " ", response.text)
        text = re.sub(r"\s+", " ", text)
        return text[:5000]
    except Exception:
        return "No website text available."


# ─── Google Sheets (storage) ─────────────────────────────────────────────────
def get_sheet():
    """Lazy-open the Leads worksheet, creating the header if empty."""
    global _sheet
    if _sheet is None:
        creds = Credentials.from_service_account_file(SHEET_CREDS, scopes=SHEET_SCOPES)
        client = gspread.authorize(creds)
        sh = client.open_by_key(SHEET_ID)  # Sheets API only; avoids Drive API dependency
        try:
            _sheet = sh.worksheet(SHEET_TAB)
        except gspread.WorksheetNotFound:
            _sheet = sh.add_worksheet(title=SHEET_TAB, rows=1000, cols=len(HEADERS))
        if not _sheet.row_values(1):
            _sheet.append_row(HEADERS)
    return _sheet


def get_leads(limit: int = 50):
    """Return leads newest-first as dicts, each tagged with its sheet row (_row)."""
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
    """True if a lead with this website already exists."""
    if not website:
        return False
    ws = get_sheet()
    vals = ws.get_all_values()
    if len(vals) < 2 or "website" not in vals[0]:
        return False
    col = vals[0].index("website")
    target = website.strip().lower()
    return any(r[col].strip().lower() == target for r in vals[1:])


def insert_lead(data: dict):
    """Append a lead row (status defaults to DRAFT_READY)."""
    row = [data.get(h, "") for h in HEADERS[:-1]]  # exclude created_at
    row.append(datetime.now(timezone.utc).isoformat())
    get_sheet().append_row(row)


def update_lead(row_id: int, fields: dict):
    """Update specific columns on a lead row (row_id = sheet row number)."""
    ws = get_sheet()
    header = ws.row_values(1)
    for k, v in fields.items():
        if k in header:
            ws.update_cell(row_id, header.index(k) + 1, v)


# ─── Gmail (sending) ────────────────────────────────────────────────────────
def send_email(to_addr: str, subject: str, body: str) -> str:
    """Send an email via Gmail SMTP using an app password. Returns a status string."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        return "ERROR: GMAIL_USER / GMAIL_APP_PASSWORD not set."
    if not to_addr:
        return "ERROR: no recipient email on file."
    msg = EmailMessage()
    msg["From"] = f"Pacific Yew Automations <{GMAIL_USER}>"
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Reply-To"] = REPLY_TO_EMAIL
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.send_message(msg)
        return "SENT"
    except Exception as e:
        return f"ERROR: {e}"


# ─── Pipeline ────────────────────────────────────────────────────────────────
def main():
    print("Waking up Pacific Yew BDR agent...")
    businesses = discover_businesses()

    for biz in businesses:
        if not biz.get("website"):
            continue
        if dedup_exists(biz.get("website")):
            print(f"Already contacted: {biz.get('title')}")
            continue

        print(f"Qualifying: {biz.get('title')}")
        agent_output = qualify_and_draft(biz)

        data = {
            "business_name": biz.get("title"),
            "website": biz.get("website"),
            "phone": biz.get("phone"),
            "email": biz.get("email", ""),
            "agent_analysis": agent_output,
            "status": "DRAFT_READY",
        }

        try:
            insert_lead(data)
            print(f"Inserted into Sheets: {biz.get('title')}")
        except Exception as e:
            print(f"Sheets insert error for {biz.get('title')}: {e}")


if __name__ == "__main__":
    main()
