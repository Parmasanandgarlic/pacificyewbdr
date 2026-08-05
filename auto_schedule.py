from __future__ import annotations

import argparse
from datetime import datetime

import schedule_control


def claim_due_slot(run_cap: int, now: datetime | None = None) -> dict[str, str]:
    """Claim the oldest unfinished slot that is still inside its recovery window.

    Frequent scheduler heartbeats call this function without needing to know which
    cron expression fired. Completed slots are skipped, while failed or missing
    slots remain recoverable up to the existing 105-minute boundary.
    """
    current = now or datetime.now(schedule_control.PACIFIC)
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-cap", type=int, default=8)
    args = parser.parse_args()
    schedule_control._write_outputs(claim_due_slot(args.run_cap))


if __name__ == "__main__":
    main()
