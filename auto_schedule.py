from __future__ import annotations

import argparse
import os
from datetime import datetime

import schedule_control


def claim_due_slot(run_cap: int, now: datetime | None = None) -> dict[str, str]:
    """Claim the oldest unfinished slot still inside its recovery window.

    The heartbeat receives a slightly wider queue-delay allowance than the
    legacy exact-slot scheduler. This lets a 9:43 candidate still recover the
    8:00 slot if GitHub starts it a few minutes late, while expiring that slot
    before the next 10:13 heartbeat.
    """
    current = now or datetime.now(schedule_control.PACIFIC)
    original_window = schedule_control.RECOVERY_WINDOW_MINUTES
    heartbeat_window = max(
        original_window,
        int(os.environ.get("HEARTBEAT_RECOVERY_WINDOW_MINUTES", "125")),
    )
    schedule_control.RECOVERY_WINDOW_MINUTES = heartbeat_window
    try:
        due_slots = [
            slot
            for slot, _minutes in sorted(
                schedule_control.SLOT_MINUTES.items(),
                key=lambda item: item[1],
            )
            if schedule_control.slot_is_due(slot, current)[0]
        ]

        for slot in due_slots:
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
    schedule_control._write_outputs(claim_due_slot(args.run_cap))


if __name__ == "__main__":
    main()
