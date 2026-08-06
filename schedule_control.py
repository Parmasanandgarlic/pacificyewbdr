from __future__ import annotations

import argparse
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, TypeVar
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
ACTIVE_WORKER_LEASE_SECONDS = 100 * 60
T = TypeVar("T")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value or "").strip())
    except (TypeError, ValueError):
        return default


def _parse_utc(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _active_worker_attempt(
    records: list[dict[str, str]],
    local_date: str,
    current_attempt_id: str,
    now_utc: datetime | None = None,
) -> dict[str, str] | None:
    """Return a fresh unfinished worker lease owned by another attempt."""
    current = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    lease_seconds = max(
        60,
        min(
            _safe_int(
                os.environ.get("ACTIVE_WORKER_LEASE_SECONDS"),
                ACTIVE_WORKER_LEASE_SECONDS,
            ),
            2 * 60 * 60,
        ),
    )
    for record in reversed(records):
        if record.get("local_date") != local_date:
            continue
        if record.get("attempt_id") == current_attempt_id:
            continue
        if (record.get("status") or "").strip().upper() != "STARTED":
            continue
        if (record.get("finished_at") or "").strip():
            continue
        claimed_at = _parse_utc(record.get("claimed_at"))
        if claimed_at is None:
            continue
        age = (current - claimed_at).total_seconds()
        if 0 <= age <= lease_seconds:
            return record
    return None


def _sheet_retry(
    label: str,
    operation: Callable[[], T],
    attempts: int = 5,
) -> T:
    """Retry idempotent Run Control and ledger operations through a quota window.

    Google Sheets read quotas reset over a rolling minute. The 4, 8, 16 and
    24-second waits span that interval without issuing rapid duplicate calls.
    Callers must only use this helper for reads or idempotent writes.
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            value = operation()
            if attempt > 1:
                print(f"[schedule-control] {label} recovered on attempt {attempt}.")
            return value
        except Exception as exc:
            last_error = exc
            print(
                f"[schedule-control] {label} attempt {attempt}/{attempts} "
                f"failed: {exc}"
            )
            if attempt < attempts:
                time.sleep(min(24, 2 ** (attempt + 1)))
    raise RuntimeError(
        f"{label} failed after {attempts} attempts: {last_error}"
    ) from last_error


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
    def authorize():
        creds = legacy.Credentials.from_service_account_file(
            legacy.SHEET_CREDS,
            scopes=legacy.SHEET_SCOPES,
        )
        client = gspread.authorize(creds)
        return client.open_by_key(legacy.SHEET_ID)

    return _sheet_retry("spreadsheet authorization", authorize)


def _ensure_control_sheet():
    spreadsheet = _authorize_spreadsheet()

    def open_or_create():
        try:
            return spreadsheet.worksheet(CONTROL_TAB)
        except gspread.WorksheetNotFound:
            return spreadsheet.add_worksheet(
                title=CONTROL_TAB,
                rows=2000,
                cols=len(CONTROL_HEADERS),
            )

    worksheet = _sheet_retry("Run Control worksheet lookup", open_or_create)
    headers = _sheet_retry("Run Control header read", lambda: worksheet.row_values(1))
    if not headers:
        _sheet_retry(
            "Run Control header initialization",
            lambda: worksheet.update("A1", [CONTROL_HEADERS]),
        )
        headers = list(CONTROL_HEADERS)
    missing = [header for header in CONTROL_HEADERS if header not in headers]
    if missing:
        _sheet_retry(
            "Run Control header reconciliation",
            lambda: worksheet.update("A1", [headers + missing]),
        )
    return worksheet


def _records(worksheet) -> tuple[list[str], list[dict[str, str]]]:
    values = _sheet_retry(
        "Run Control records read",
        lambda: worksheet.get_all_values(),
    )
    headers = values[0] if values else list(CONTROL_HEADERS)
    records: list[dict[str, str]] = []
    for row_number, row in enumerate(values[1:], start=2):
        record = dict(zip(headers, row + [""] * (len(headers) - len(row))))
        record["_row"] = str(row_number)
        records.append(record)
    return headers, records


def _sent_ledger_count() -> int:
    def read() -> list[list[str]]:
        return legacy._ensure_ledger().get_all_values()

    rows = _sheet_retry("Sent Ledger count read", read)
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


def _append_attempt_row(
    worksheet,
    row: list[str],
    attempt_id: str,
    known_records: list[dict[str, str]] | None = None,
    attempts: int = 5,
) -> None:
    """Append a Run Control attempt once, even if Sheets returns ambiguously.

    A timed-out append can have reached Sheets before the client sees an error.
    Before every retry, refresh the ledger and reuse the existing attempt row.
    The deterministic attempt ID makes this safe across process reruns too.
    """
    if any(record.get("attempt_id") == attempt_id for record in (known_records or [])):
        return

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            _headers, refreshed = _records(worksheet)
            if any(record.get("attempt_id") == attempt_id for record in refreshed):
                print(
                    f"[schedule-control] recovered existing attempt after "
                    f"ambiguous append: {attempt_id}"
                )
                return
        try:
            worksheet.append_row(row)
            return
        except Exception as exc:
            last_error = exc
            print(
                f"[schedule-control] Run Control append attempt "
                f"{attempt}/{attempts} failed: {exc}"
            )
            if attempt < attempts:
                time.sleep(min(24, 2 ** (attempt + 1)))
    raise RuntimeError(
        f"Run Control append failed after {attempts} attempts: {last_error}"
    ) from last_error


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
    run_identifier = os.environ.get("GITHUB_RUN_ID") or uuid.uuid4().hex[:12]
    attempt_id = f"{local_date}-{slot}-{run_identifier}"

    worksheet = _ensure_control_sheet()
    _headers, records = _records(worksheet)
    active_worker = _active_worker_attempt(records, local_date, attempt_id)
    if active_worker is not None:
        return {
            "should_run": "false",
            "run_slot": slot,
            "send_limit": "0",
            "attempt_id": "",
            "reason": "active_worker_lease",
        }

    attempts = [
        row for row in records
        if row.get("local_date") == local_date and row.get("run_slot") == slot
    ]

    # Attempt status describes that individual process, not whether the whole
    # slot reached capacity. A successful zero-send or partial-send attempt must
    # remain recoverable. The immutable Sent Ledger is the authority for how
    # much of the slot was actually filled.
    current_ledger_count = _sent_ledger_count()
    prior_sent = prior_slot_sent_count(attempts, current_ledger_count)
    remaining = max(0, max(1, run_cap) - prior_sent)
    if remaining <= 0:
        reconciled_id = f"{local_date}-{slot}-cap-{max(1, run_cap)}"
        _append_attempt_row(
            worksheet,
            [
                local_date,
                slot,
                reconciled_id,
                "COMPLETED",
                _utc_now(),
                _utc_now(),
                str(current_ledger_count),
                "0",
                "reconciled prior attempts at slot cap",
                os.environ.get("GITHUB_RUN_ID", ""),
            ],
            reconciled_id,
            records,
        )
        return {
            "should_run": "false",
            "run_slot": slot,
            "send_limit": "0",
            "attempt_id": "",
            "reason": "slot_cap_already_reached",
        }

    existing_attempt = next(
        (row for row in attempts if row.get("attempt_id") == attempt_id),
        None,
    )
    if existing_attempt is not None:
        return {
            "should_run": "true",
            "run_slot": slot,
            "send_limit": str(remaining),
            "attempt_id": attempt_id,
            "reason": "slot_claim_recovered",
        }

    _append_attempt_row(
        worksheet,
        [
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
        ],
        attempt_id,
        records,
    )
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
    _sheet_retry(
        "Run Control finalization update",
        lambda: worksheet.update_cells(cells),
    )
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
