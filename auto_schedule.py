from __future__ import annotations

import argparse
import os
from datetime import datetime

import schedule_control


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() == "true"


def _cutoff_minutes() -> int:
    raw = os.environ.get("SAME_DAY_CATCHUP_CUTOFF_MINUTES", "1080")
    try:
        return max(14 * 60, min(int(raw), 18 * 60))
    except (TypeError, ValueError):
        return 18 * 60


def emergency_recovery_is_exclusive(now: datetime | None = None) -> bool:
    """Keep stale workflow reruns from racing today's isolated recovery worker.

    The guard is deliberately date-scoped and expires automatically after the
    Aug 6 incident. The isolated workflow must opt in explicitly. Normal
    scheduling resumes without a code change on the next Pacific date.
    """
    current = (now or datetime.now(schedule_control.PACIFIC)).astimezone(
        schedule_control.PACIFIC
    )
    incident_date = os.environ.get("BDR_EMERGENCY_RECOVERY_DATE", "2026-08-06")
    return (
        current.date().isoformat() == incident_date
        and current.hour * 60 + current.minute <= _cutoff_minutes()
        and not _bool_env("BDR_EMERGENCY_DRAIN", False)
    )


def claim_due_slot(run_cap: int, now: datetime | None = None) -> dict[str, str]:
    """Claim the oldest unfinished scheduled slot that may still run today.

    Normal heartbeats retain the narrow recovery window. When bounded same-day
    catch-up is enabled, an elapsed slot that GitHub never launched can still be
    recovered before the Pacific cutoff. Run Control and the immutable Sent
    Ledger remain authoritative for per-slot completion and remaining capacity.
    """
    current = (now or datetime.now(schedule_control.PACIFIC)).astimezone(
        schedule_control.PACIFIC
    )
    current_minutes = current.hour * 60 + current.minute
    allow_catchup = (
        _bool_env("ALLOW_SAME_DAY_CATCHUP", True)
        and current.weekday() < 5
        and current_minutes <= _cutoff_minutes()
    )

    original_window = schedule_control.RECOVERY_WINDOW_MINUTES
    heartbeat_window = max(
        original_window,
        int(os.environ.get("HEARTBEAT_RECOVERY_WINDOW_MINUTES", "125")),
    )
    if allow_catchup:
        elapsed_delays = [
            current_minutes - slot_minutes
            for slot_minutes in schedule_control.SLOT_MINUTES.values()
            if current_minutes >= slot_minutes
        ]
        if elapsed_delays:
            heartbeat_window = max(heartbeat_window, max(elapsed_delays))

    schedule_control.RECOVERY_WINDOW_MINUTES = heartbeat_window
    try:
        elapsed_slots = [
            slot
            for slot, slot_minutes in sorted(
                schedule_control.SLOT_MINUTES.items(),
                key=lambda item: item[1],
            )
            if current_minutes >= slot_minutes
            and schedule_control.slot_is_due(slot, current)[0]
        ]

        for slot in elapsed_slots:
            result = schedule_control.claim_slot(slot, run_cap, current)
            if result.get("should_run") == "true":
                return result
            if result.get("reason") in {
                "slot_already_completed",
                "slot_cap_already_reached",
            }:
                continue
            return result

        return {
            "should_run": "false",
            "run_slot": "none",
            "send_limit": "0",
            "attempt_id": "",
            "reason": "no_due_slot",
        }
    finally:
        schedule_control.RECOVERY_WINDOW_MINUTES = original_window


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-cap", type=int, default=8)
    args = parser.parse_args()
    if emergency_recovery_is_exclusive():
        schedule_control._write_outputs({
            "should_run": "false",
            "run_slot": "none",
            "send_limit": "0",
            "attempt_id": "",
            "reason": "isolated_emergency_recovery_active",
        })
        return
    schedule_control._write_outputs(claim_due_slot(args.run_cap))


if __name__ == "__main__":
    main()
