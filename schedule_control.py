from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import gspread

import bdr_agent as legacy

PACIFIC = ZoneInfo("America/Vancouver")
CONTROL_TAB = os.environ.get("RUN_CONTROL_TAB", "Run Control")
CONTROL_HEADERS = [
    "local_date",
    "run_slot",
    "attempt_id",
    "status",
    "claimed_at",
    "finished_at",
    "sent_ledger_count_at_claim",
    "sent_count",
    "error",
    "github_run_id",
]
SLOT_MINUTES = {
    "morning": 8 * 60,
    "late_morning": 10 * 60,
    "midday": 12 * 60,
    "afternoon": 14 * 60,
}
RECOVERY_WINDOW_MINUTES = 105


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value or "").strip())
    except (TypeError, ValueError):
        return default


def slot_is_due(slot: str, now: datetime | None = None) -> tuple[bool, str]:
    expected = SLOT_MINUTES.get(slot)
    if expected is None:
        return False, f"unknown scheduled slot: {slot}"
    local_now = (now or datetime.now(PACIFIC)).astimezone(PACIFIC)
    if local_now.weekday() >= 5:
        return False, "scheduled outreach is limited to weekdays"
    current = local_now.hour * 60 + local_now.minute
    delay = current - expected
    if delay < 0:
        return False, f"slot {slot} is not due yet"
    if delay > RECOVERY_WINDOW_MINUTES:
        return False, f"slot {slot} recovery window expired ({delay} minutes late)"
    return True, "due"


def prior_slot_sent_count(attempts: list[dict[str, str]], current_ledger_count: int) -> int:
    if not attempts:
        return 0
    recorded = sum(max(0, _safe_int(row.get("sent_count"))) for row in attempts)
    baselines = [
        _safe_int(row.get("sent_ledger_count_at_claim"), -1)
        for row in attempts
        if str(row.get("sent_ledger_count_at_claim", "")).strip()
    ]
    baselines = [value for value in baselines if value >= 0]
    reconciled = max(0, current_ledger_count - min(baselines)) if baselines else 0
    return max(recorded, reconciled)


def _authorize_spreadsheet():
    creds = legacy.Credentials.from_service_account_file(
        legacy.SHEET_CREDS,
        scopes=legacy.SHEET_SCOPES,
    )
    client = gspread.authorize(creds)
    return client.open_by_key(legacy.SHEET_ID)


def _ensure_control_sheet():
    spreadsheet = _authorize_spreadsheet()
    try:
        worksheet = spreadsheet.worksheet(CONTROL_TAB)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=CONTROL_TAB,
            rows=2000,
            cols=len(CONTROL_HEADERS),
        )
        worksheet.append_row(CONTROL_HEADERS)
    headers = worksheet.row_values(1)
    if not headers:
        worksheet.append_row(CONTROL_HEADERS)
        headers = list(CONTROL_HEADERS)
    missing = [header for header in CONTROL_HEADERS if header not in headers]
    if missing:
        worksheet.update("A1", [headers + missing])
    return worksheet


def _records(worksheet) -> tuple[list[str], list[dict[str, str]]]:
    values = worksheet.get_all_values()
    headers = values[0] if values else list(CONTROL_HEADERS)
    records: list[dict[str, str]] = []
    for row_number, row in enumerate(values[1:], start=2):
        record = dict(zip(headers, row + [""] * (len(headers) - len(row))))
        record["_row"] = str(row_number)
        records.append(record)
    return headers, records


def _sent_ledger_count() -> int:
    rows = legacy._ensure_ledger().get_all_values()
    if not rows:
        raise RuntimeError("Sent Ledger is empty or unreadable")
    headers = rows[0]
    if "email" not in headers or "sent_at" not in headers:
        raise RuntimeError("Sent Ledger is missing required email/sent_at columns")
    email_index = headers.index("email")
    sent_index = headers.index("sent_at")
    return sum(
        1
        for row in rows[1:]
        if len(row) > max(email_index, sent_index)
        and row[email_index].strip()
        and row[sent_index].strip()
    )


def _write_outputs(values: dict[str, str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    lines = [f"{key}={value}" for key, value in values.items()]
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output:
            output.write("\n".join(lines) + "\n")
    for line in lines:
        print(f"[schedule-control] {line}")


def claim_slot(slot: str, run_cap: int, now: datetime | None = None) -> dict[str, str]:
    due, reason = slot_is_due(slot, now)
    if not due:
        return {
            "should_run": "false",
            "run_slot": slot,
            "send_limit": "0",
            "attempt_id": "",
            "reason": re.sub(r"\s+", "_", reason),
        }

    local_now = (now or datetime.now(PACIFIC)).astimezone(PACIFIC)
    local_date = local_now.date().isoformat()
    worksheet = _ensure_control_sheet()
    _headers, records = _records(worksheet)
    attempts = [
        row for row in records
        if row.get("local_date") == local_date and row.get("run_slot") == slot
    ]
    if any((row.get("status") or "").upper() == "COMPLETED" for row in attempts):
        return {
            "should_run": "false",
            "run_slot": slot,
            "send_limit": "0",
            "attempt_id": "",
            "reason": "slot_already_completed",
        }

    current_ledger_count = _sent_ledger_count()
    prior_sent = prior_slot_sent_count(attempts, current_ledger_count)
    remaining = max(0, max(1, run_cap) - prior_sent)
    if remaining <= 0:
        worksheet.append_row([
            local_date,
            slot,
            f"reconciled-{uuid.uuid4().hex[:12]}",
            "COMPLETED",
            _utc_now(),
            _utc_now(),
            str(current_ledger_count),
            "0",
            "reconciled prior attempts at slot cap",
            os.environ.get("GITHUB_RUN_ID", ""),
        ])
        return {
            "should_run": "false",
            "run_slot": slot,
            "send_limit": "0",
            "attempt_id": "",
            "reason": "slot_cap_already_reached",
        }

    attempt_id = f"{local_date}-{slot}-{os.environ.get('GITHUB_RUN_ID', uuid.uuid4().hex[:12])}"
    worksheet.append_row([
        local_date,
        slot,
        attempt_id,
        "STARTED",
        _utc_now(),
        "",
        str(current_ledger_count),
        "",
        "",
        os.environ.get("GITHUB_RUN_ID", ""),
    ])
    return {
        "should_run": "true",
        "run_slot": slot,
        "send_limit": str(remaining),
        "attempt_id": attempt_id,
        "reason": "slot_claimed",
    }


def finish_slot(attempt_id: str, success: bool, error: str = "") -> int:
    if not attempt_id:
        return 0
    worksheet = _ensure_control_sheet()
    headers, records = _records(worksheet)
    matching = [row for row in records if row.get("attempt_id") == attempt_id]
    if not matching:
        raise RuntimeError(f"Run Control attempt not found: {attempt_id}")
    record = matching[-1]
    row_number = _safe_int(record.get("_row"))
    baseline = _safe_int(record.get("sent_ledger_count_at_claim"))
    sent_count = max(0, _sent_ledger_count() - baseline)
    updates = {
        "status": "COMPLETED" if success else "FAILED",
        "finished_at": _utc_now(),
        "sent_count": str(sent_count),
        "error": re.sub(r"\s+", " ", error or "")[:500],
    }
    cells = [
        gspread.Cell(row=row_number, col=headers.index(field) + 1, value=value)
        for field, value in updates.items()
    ]
    worksheet.update_cells(cells)
    print(
        f"[schedule-control] finished attempt={attempt_id} "
        f"status={updates['status']} sent={sent_count}"
    )
    return sent_count


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    claim = subparsers.add_parser("claim")
    claim.add_argument("--slot", required=True, choices=sorted(SLOT_MINUTES))
    claim.add_argument("--run-cap", type=int, default=8)
    finish = subparsers.add_parser("finish")
    finish.add_argument("--attempt-id", required=True)
    finish.add_argument("--success", choices=("true", "false"), required=True)
    finish.add_argument("--error", default="")
    args = parser.parse_args()

    if args.command == "claim":
        outputs = claim_slot(args.slot, args.run_cap)
        _write_outputs(outputs)
        return
    finish_slot(args.attempt_id, args.success == "true", args.error)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[schedule-control] fatal: {exc}", file=sys.stderr)
        raise
