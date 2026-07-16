import os
import re
import streamlit as st
from dotenv import load_dotenv

# Load .env before anything touches env vars
load_dotenv()

from bdr_agent import (
    discover_businesses,
    analyze_and_draft,
    scrape_website,
    scrape_email,
    get_leads,
    dedup_exists,
    insert_lead,
    update_lead,
    send_email,
)

# ─── Page Config ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Pacific Yew · BDR Command Center",
    page_icon="🌲",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Custom CSS ──────────────────────────────────────────────────────────────
st.markdown(
    """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

    :root {
        --py-bg: #0a0f1a;
        --py-surface: #111827;
        --py-surface-hover: #1a2332;
        --py-border: #1e293b;
        --py-accent: #10b981;
        --py-accent-dim: rgba(16,185,129,0.15);
        --py-text: #e2e8f0;
        --py-text-muted: #94a3b8;
        --py-danger: #ef4444;
        --py-warning: #f59e0b;
    }

    .stApp { font-family: 'Inter', sans-serif !important; }

    section[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #0f1729 0%, #111827 100%) !important;
        border-right: 1px solid var(--py-border) !important;
    }
    section[data-testid="stSidebar"] .stMarkdown h1 {
        background: linear-gradient(135deg, #10b981, #34d399);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        font-weight: 700;
        letter-spacing: -0.02em;
    }

    div[data-testid="stExpander"] {
        border: 1px solid var(--py-border) !important;
        border-radius: 12px !important;
        overflow: hidden;
    }

    .stButton > button {
        border-radius: 8px !important;
        font-weight: 600 !important;
        font-family: 'Inter', sans-serif !important;
        letter-spacing: 0.01em;
        transition: all 0.2s ease !important;
        border: 1px solid var(--py-border) !important;
    }
    .stButton > button:hover {
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(16,185,129,0.2);
    }

    .approve-btn button {
        background: linear-gradient(135deg, #059669, #10b981) !important;
        color: white !important;
        border: none !important;
    }
    .reject-btn button {
        background: transparent !important;
        color: var(--py-danger) !important;
        border: 1px solid var(--py-danger) !important;
    }

    .status-badge {
        display: inline-block;
        padding: 4px 12px;
        border-radius: 9999px;
        font-size: 0.75rem;
        font-weight: 600;
        letter-spacing: 0.05em;
        text-transform: uppercase;
    }
    .status-draft { background: rgba(245,158,11,0.15); color: #f59e0b; }
    .status-approved { background: rgba(16,185,129,0.15); color: #10b981; }
    .status-rejected { background: rgba(239,68,68,0.15); color: #ef4444; }
    .status-sent { background: rgba(59,130,246,0.15); color: #3b82f6; }
    .status-needs { background: rgba(148,163,184,0.15); color: #94a3b8; }

    .metric-card {
        background: linear-gradient(135deg, #111827, #1a2332);
        border: 1px solid var(--py-border);
        border-radius: 12px;
        padding: 20px;
        text-align: center;
    }
    .metric-card .metric-value {
        font-size: 2rem;
        font-weight: 700;
        background: linear-gradient(135deg, #10b981, #34d399);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
    }
    .metric-card .metric-label {
        font-size: 0.8rem;
        color: var(--py-text-muted);
        text-transform: uppercase;
        letter-spacing: 0.08em;
        margin-top: 4px;
    }

    div[data-testid="stRadio"] label { transition: all 0.15s ease; }
    div[data-testid="stRadio"] label:hover { color: #10b981 !important; }

    hr { border-color: var(--py-border) !important; opacity: 0.5; }

    .stTextArea textarea {
        border-radius: 8px !important;
        border-color: var(--py-border) !important;
        font-family: 'Inter', monospace !important;
        font-size: 0.9rem !important;
        line-height: 1.6 !important;
    }
    .stSelectbox > div > div { border-radius: 8px !important; }

    #MainMenu { visibility: hidden; }
    footer { visibility: hidden; }
    header { visibility: hidden; }
</style>
""",
    unsafe_allow_html=True,
)


def get_status_badge(status: str) -> str:
    css_class = {
        "DRAFT_READY": "status-draft",
        "APPROVED": "status-approved",
        "REJECTED": "status-rejected",
        "SENT": "status-sent",
        "NEEDS_EMAIL": "status-needs",
        "NEEDS_DRAFT": "status-needs",
    }.get(status, "status-draft")
    return f'<span class="status-badge {css_class}">{status}</span>'


# ─── Sidebar ─────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("# 🌲 Pacific Yew")
    st.caption("BDR Command Center")
    st.markdown("---")

    st.subheader("⚡ Manual Agent Trigger")

    search_query = st.text_input(
        "Search Query",
        value="HVAC company Vancouver",
        placeholder="e.g. dental clinic Surrey",
        help="Web search string for discovery (free DuckDuckGo).",
    )

    if st.button("🎯 Run Agent Now", use_container_width=True, type="primary"):
        with st.spinner("Agent is hunting..."):
            businesses = discover_businesses(search_query)
            inserted = 0
            skipped = 0

            for biz in businesses:
                if not biz.get("website"):
                    skipped += 1
                    continue
                if dedup_exists(biz.get("website")):
                    skipped += 1
                    continue

                # Unified path: scrape site + email, then draft (new plain voice)
                website_text = scrape_website(biz.get("website", ""))
                email = scrape_email(biz.get("website", ""))
                draft = analyze_and_draft(biz, website_text)

                data = {
                    "business_name": biz.get("title"),
                    "website": biz.get("website"),
                    "phone": biz.get("phone", ""),
                    "email": email,
                    "agent_analysis": draft.get("qualified", ""),
                    "email_subject": draft.get("subject", ""),
                    "email_body": draft.get("body", ""),
                    "status": "DRAFT_READY" if email else "NEEDS_EMAIL",
                }
                try:
                    insert_lead(data)
                    inserted += 1
                except Exception as e:
                    st.error(f"Insert failed: {biz.get('title')} — {e}")

        st.success(f"✅ Done! {inserted} new leads added, {skipped} skipped.")
        st.rerun()

    st.markdown("---")
    st.caption("Pacific Yew · Automation for local service businesses")
    st.caption("v2.0 · Command Center")


# ─── Main Area ────────────────────────────────────────────────────────────────
st.markdown("## 📋 Lead Queue & Draft Review")

all_leads = get_leads(limit=200)

draft_count = sum(1 for l in all_leads if l.get("status") == "DRAFT_READY")
approved_count = sum(1 for l in all_leads if l.get("status") == "APPROVED")
rejected_count = sum(1 for l in all_leads if l.get("status") == "REJECTED")
sent_count = sum(1 for l in all_leads if l.get("status") == "SENT")

col_m1, col_m2, col_m3, col_m4 = st.columns(4)
with col_m1:
    st.markdown(f"""<div class="metric-card"><div class="metric-value">{len(all_leads)}</div><div class="metric-label">Total Leads</div></div>""", unsafe_allow_html=True)
with col_m2:
    st.markdown(f"""<div class="metric-card"><div class="metric-value">{draft_count}</div><div class="metric-label">Drafts Ready</div></div>""", unsafe_allow_html=True)
with col_m3:
    st.markdown(f"""<div class="metric-card"><div class="metric-value">{approved_count}</div><div class="metric-label">Approved</div></div>""", unsafe_allow_html=True)
with col_m4:
    st.markdown(f"""<div class="metric-card"><div class="metric-value">{sent_count}</div><div class="metric-label">Sent</div></div>""", unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

# Reviewable = anything not yet sent/rejected
reviewable = [l for l in all_leads if l.get("status") not in ("SENT", "REJECTED")]

if not reviewable:
    st.info("🔍 No leads to review. Run the agent from the sidebar to discover new leads.")
    st.stop()

lead_options = {
    f"{l['business_name']}  ·  {l.get('website', 'N/A')}  ·  {l.get('status', 'DRAFT_READY')}": l
    for l in reviewable
}

selected_label = st.selectbox(
    "Select a lead to review",
    options=list(lead_options.keys()),
    index=0,
)
lead = lead_options[selected_label]
lead_row = lead.get("_row")

st.markdown("---")

col_left, col_right = st.columns([1, 1], gap="large")

with col_left:
    st.markdown("### 🏢 Lead Details")
    st.markdown(get_status_badge(lead.get("status", "DRAFT_READY")), unsafe_allow_html=True)
    st.markdown(f"**Business:** {lead.get('business_name', 'N/A')}")

    website = lead.get("website", "")
    if website:
        display_url = website if website.startswith("http") else f"https://{website}"
        st.markdown(f"**Website:** [{website}]({display_url})")
    else:
        st.markdown("**Website:** N/A")

    st.markdown(f"**Phone:** {lead.get('phone', 'N/A')}")
    st.markdown(f"**Email:** {lead.get('email', 'N/A')}")
    if not lead.get("email"):
        st.warning("No email scraped — send will be skipped until one is added.", icon="⚠️")

    with st.expander("📄 Qualification Note", expanded=False):
        st.text(lead.get("agent_analysis", "No analysis available."))

with col_right:
    st.markdown("### ✉️ Email Draft Editor")

    subject = lead.get("email_subject", "") or lead.get("business_name", "")
    body = lead.get("email_body", "") or lead.get("agent_analysis", "")

    edited_subject = st.text_input("Subject", value=subject, key=f"subj_{lead_row}")
    edited_draft = st.text_area(
        "Edit the email body below:",
        value=body,
        height=320,
        key=f"draft_{lead_row}",
        label_visibility="collapsed",
    )

st.markdown("<br>", unsafe_allow_html=True)
btn_col1, btn_col2, btn_col3, btn_col4 = st.columns([1, 1, 1, 1])

with btn_col1:
    st.markdown('<div class="approve-btn">', unsafe_allow_html=True)
    if st.button("✅ Approve", use_container_width=True, key="btn_approve"):
        # Save edits, mark APPROVED (does NOT send). Sending happens via the
        # daily GitHub run (approve-first) or the Send Approved button below.
        update_lead(lead_row, {
            "status": "APPROVED",
            "email_subject": edited_subject,
            "email_body": edited_draft,
        })
        st.toast("Lead approved — will be sent on next dispatch.", icon="✅")
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)

with btn_col2:
    if st.button("📤 Send Now", use_container_width=True, key="btn_send"):
        # Direct send from UI (human-in-the-loop). CASL gate still enforced in send_email.
        to = (lead.get("email") or "").strip()
        if not to:
            st.toast("No email on file — cannot send.", icon="⚠️")
        else:
            result = send_email(to, edited_subject, edited_draft)
            if result == "SENT":
                update_lead(lead_row, {
                    "status": "SENT",
                    "email_subject": edited_subject,
                    "email_body": edited_draft,
                    "sent_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
                })
                st.toast("Sent! 🎉", icon="✅")
            else:
                st.toast(f"Send failed: {result}", icon="⚠️")
        st.rerun()

with btn_col3:
    st.markdown('<div class="reject-btn">', unsafe_allow_html=True)
    if st.button("❌ Reject", use_container_width=True, key="btn_reject"):
        update_lead(lead_row, {"status": "REJECTED"})
        st.toast("Lead rejected.", icon="❌")
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)

with btn_col4:
    if st.button("💾 Save Edits", use_container_width=True, key="btn_save"):
        update_lead(lead_row, {
            "email_subject": edited_subject,
            "email_body": edited_draft,
        })
        st.toast("Edits saved!", icon="💾")
        st.rerun()
