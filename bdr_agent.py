import os
import re
import requests
from supabase import create_client, Client
import google.generativeai as genai

# --- Lazy-initialized globals ---
_supabase: Client = None
_model = None

MODEL_NAME = "gemini-3.1-flash-lite"

# Disable Google's safety filters that sometimes block BDR outreach
SAFETY_SETTINGS = {
    "HARASSMENT": "BLOCK_NONE",
    "HATE_SPEECH": "BLOCK_NONE",
    "SEXUALLY_EXPLICIT": "BLOCK_NONE",
    "DANGEROUS_CONTENT": "BLOCK_NONE",
}

PACIFIC_YEW_ICP = """
Target: Vancouver/Surrey high-end B2B firms (wealth advisors, luxury real estate, clinics).
Pain Point: Drowning in manual admin, using basic CRMs (or none), missing follow-ups.
Our Solution: "Relationship Intelligence OS" & AI automation (We don't build Zapier wrappers; we build the underlying data graph).
Tone: Quiet Operator. Professional, direct, zero hype.
"""


def get_supabase() -> Client:
    """Lazy-initialize the Supabase client so imports don't crash without env vars."""
    global _supabase
    if _supabase is None:
        url: str = os.environ.get("SUPABASE_URL")
        key: str = os.environ.get("SUPABASE_KEY")
        _supabase = create_client(url, key)
    return _supabase


def get_model():
    """Lazy-initialize the Gemini model."""
    global _model
    if _model is None:
        genai.configure(api_key=os.environ.get("GOOGLE_API_KEY"))
        _model = genai.GenerativeModel(MODEL_NAME)
    return _model


def discover_businesses(search_query: str = "wealth management firm Vancouver"):
    """Calls Apify Google Maps scraper to find local businesses"""
    token = os.environ.get("APIFY_TOKEN")
    api_url = f"https://api.apify.com/v2/acts/apify~google-maps-scraper/run-sync-get-dataset-items?token={token}"

    payload = {
        "searchStrings": [search_query],
        "maxCrawledPlacesPerSearch": 5,
    }

    response = requests.post(api_url, json=payload)
    if response.status_code == 200:
        return response.json()
    else:
        print(f"Apify error: {response.status_code} - {response.text}")
        return []


def scrape_website(website_url):
    """Quickly pull text from the homepage to check their tech stack"""
    try:
        if not website_url.startswith("http"):
            website_url = "https://" + website_url

        response = requests.get(website_url, timeout=5)
        # Simple HTML stripping to save LLM tokens
        text = re.sub(r"<[^>]+>", " ", response.text)
        text = re.sub(r"\s+", " ", text)
        return text[:5000]  # Gemini can handle more, but 5k is enough for a homepage
    except Exception as e:
        return "No website text available."


def qualify_and_draft(business):
    """Uses Gemini Flash Lite to qualify the lead and draft the email"""
    website_text = scrape_website(business.get("website", ""))

    prompt = f"""
    You are a fractional COO and BDR for Pacific Yew (pacificyew.pro).
    {PACIFIC_YEW_ICP}

    Business Data: {business}
    Website Text: {website_text}

    Task 1: Qualify. Based on the data, is this a good fit for a $10k-$15k Internal OS package? (Yes/No and why).
    Task 2: Draft. If yes, write a 4-sentence cold email to {business.get('title')}.
    - Hook: Mention something specific about their firm from the website.
    - Credibility: Mention we build event-driven architecture (MCP, Supabase) for relationship-based businesses.
    - Differentiation: We aren't a marketing agency; we build the internal data graph.
    - CTA: Ask for a 15-min discovery call.
    """

    model = get_model()
    try:
        response = model.generate_content(
            prompt,
            safety_settings=SAFETY_SETTINGS,
            generation_config=genai.types.GenerationConfig(temperature=0.7),
        )
        return response.text
    except Exception as e:
        print(f"Gemini API error: {e}")
        return "Error generating draft."


def main():
    print("Waking up Zero-Cost BDR agent...")
    sb = get_supabase()
    businesses = discover_businesses()

    for biz in businesses:
        if not biz.get("website"):
            continue  # Skip if no website

        # Check Supabase if we already contacted them using their website URL
        existing = (
            sb.table("leads")
            .select("*")
            .eq("website", biz.get("website"))
            .execute()
        )
        if existing.data:
            print(f"Already contacted: {biz.get('title')}")
            continue

        print(f"Qualifying: {biz.get('title')}")
        agent_output = qualify_and_draft(biz)

        # Save to Supabase
        data = {
            "business_name": biz.get("title"),
            "website": biz.get("website"),
            "phone": biz.get("phone"),
            "agent_analysis": agent_output,
            "status": "DRAFT_READY",
        }

        try:
            sb.table("leads").insert(data).execute()
            print(f"Inserted into Supabase: {biz.get('title')}")
        except Exception as e:
            print(f"Supabase insert error for {biz.get('title')}: {e}")


if __name__ == "__main__":
    main()
