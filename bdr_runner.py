from __future__ import annotations

import gspread

import bdr_agent as legacy
from lead_intelligence import (
    QUALIFICATION_THRESHOLD,
    analyze_and_draft,
    build_research_dossier,
    parse_qualification,
    validate_draft,
)


def intelligent_scrape(website_url: str) -> str:
    return build_research_dossier(website_url, robots_allows=legacy.robots_allows)


def intelligent_draft(business: dict, dossier: str) -> dict[str, str]:
    return analyze_and_draft(business, dossier, llm_call=legacy._or_chat)


def governed_auto_approve() -> int:
    """Approve only legally eligible, commercially qualified, quality-checked drafts."""
    approved = 0
    disqualified = 0
    needs_review = 0
    try:
        ws = legacy.get_sheet()
        legacy._sheets_throttle()
        values = ws.get_all_values()
        if len(values) < 2:
            return 0
        headers = values[0]
        required = {
            "email",
            "status",
            "source_url",
            "consent_type",
            "agent_analysis",
            "email_subject",
            "email_body",
        }
        if not required.issubset(headers):
            print(f"[intelligence-gate] missing sheet columns: {sorted(required - set(headers))}")
            return 0

        index = {name: headers.index(name) for name in required}
        updates: list[gspread.Cell] = []
        for row_number, row in enumerate(values[1:], start=2):
            def value(name: str) -> str:
                position = index[name]
                return (row[position] if len(row) > position else "").strip()

            if value("status").upper() != "DRAFT_READY":
                continue
            email = value("email")
            source_url = value("source_url")
            consent = value("consent_type").upper()
            analysis = value("agent_analysis")
            subject = value("email_subject")
            body = value("email_body")

            legally_eligible = (
                legacy.is_business_email(email)
                and consent == "IMPLIED_CONSPICUOUS"
                and source_url.startswith(("http://", "https://"))
                and not legacy.is_blocked(email)
                and not legacy._in_sent_ledger(email)
            )
            if not legally_eligible:
                continue

            qualified, score = parse_qualification(analysis)
            if not qualified:
                updates.append(
                    gspread.Cell(row=row_number, col=index["status"] + 1, value="DISQUALIFIED")
                )
                disqualified += 1
                print(f"[intelligence-gate] {email} -> DISQUALIFIED (score={score}).")
                continue

            quality_ok, quality_reason = validate_draft(subject, body)
            if not quality_ok:
                updates.append(
                    gspread.Cell(row=row_number, col=index["status"] + 1, value="NEEDS_REVIEW")
                )
                needs_review += 1
                print(f"[intelligence-gate] {email} -> NEEDS_REVIEW ({quality_reason}).")
                continue

            updates.append(gspread.Cell(row=row_number, col=index["status"] + 1, value="APPROVED"))
            approved += 1
            print(
                f"[intelligence-gate] {email} -> APPROVED "
                f"(commercial score={score}, threshold={QUALIFICATION_THRESHOLD})."
            )

        if updates:
            legacy._sheets_throttle()
            ws.update_cells(updates)
            legacy._CONTACTED_CACHE = None
    except Exception as exc:
        print(f"[intelligence-gate] error: {exc}")
        return 0

    print(
        "governed_auto_approve: "
        f"approved={approved}, disqualified={disqualified}, needs_review={needs_review}."
    )
    return approved


def install() -> None:
    legacy.scrape_website = intelligent_scrape
    legacy.analyze_and_draft = intelligent_draft
    legacy.auto_approve_qualified = governed_auto_approve


if __name__ == "__main__":
    install()
    legacy.main()
