from __future__ import annotations

import os
from uuid import UUID

import streamlit as st

from bdr_v3.postgres_repository import PostgresRepository


st.set_page_config(
    page_title="Pacific Yew BDR v3",
    page_icon="🌲",
    layout="wide",
)
st.title("Pacific Yew BDR v3 Command Center")
st.caption(
    "Evidence-backed qualification, human approvals, governed delivery, "
    "reply routing, and opportunities."
)


def repository() -> PostgresRepository:
    database_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        st.error("DATABASE_URL is not configured.")
        st.stop()
    return PostgresRepository.from_database_url(database_url)


repo = repository()
brief = repo.daily_briefing()
columns = st.columns(7)
metrics = (
    ("Researched 24h", "accounts_researched_24h"),
    ("Eligible", "eligible_accounts"),
    ("Awaiting approval", "pending_approvals"),
    ("Due", "due_touches"),
    ("Replies 24h", "replies_24h"),
    ("Open opportunities", "open_opportunities"),
    ("Suppressions 24h", "suppressions_24h"),
)
for column, (label, key) in zip(columns, metrics):
    column.metric(label, brief.get(key, 0))

approvals_tab, opportunities_tab = st.tabs(["Approval Queue", "Opportunities"])

with approvals_tab:
    st.subheader("Touches awaiting human approval")
    st.info(
        "This interface can approve a touch, but it cannot send directly. "
        "Delivery occurs only through the guarded v3 dispatcher."
    )
    approvals = repo.list_pending_approvals(100)
    if not approvals:
        st.success("No touches are waiting for approval.")
    for item in approvals:
        title = (
            f"{item['account_name']} · {item['recommended_offer']} · "
            f"score {item['total_score']} / risk {item['risk_score']} · "
            f"step {item['step_position']}"
        )
        with st.expander(title):
            st.write(f"**Contact:** {item['contact_email']}")
            st.write(f"**Scheduled:** {item['scheduled_for']}")
            st.text_input(
                "Subject",
                value=item["subject"],
                disabled=True,
                key=f"subject-{item['touch_id']}",
            )
            st.text_area(
                "Body",
                value=item["body"],
                height=260,
                disabled=True,
                key=f"body-{item['touch_id']}",
            )
            confirmed = st.checkbox(
                "I reviewed the evidence, recipient, consent basis, offer route, "
                "subject, and body.",
                key=f"confirm-{item['touch_id']}",
            )
            if st.button(
                "Approve Touch",
                disabled=not confirmed,
                key=f"approve-{item['touch_id']}",
            ):
                repo.approve_touch(
                    UUID(str(item["touch_id"])),
                    os.environ.get("BDR_REVIEWER", "streamlit-human"),
                )
                st.success("Touch approved for the guarded dispatcher.")
                st.rerun()

with opportunities_tab:
    st.subheader("Qualified conversations and next actions")
    opportunities = repo.list_opportunities(200)
    if not opportunities:
        st.info("No opportunities have been created yet.")
    else:
        st.dataframe(opportunities, use_container_width=True, hide_index=True)
