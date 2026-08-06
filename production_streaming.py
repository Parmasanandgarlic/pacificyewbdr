from __future__ import annotations

import os
import time

import auto_schedule
import bdr_agent as legacy
import discovery_reliability
import email_copy_intelligence
import fit_scoring_hotfix
import outreach_compliance
import production_hardening as hardening
import run_reliability
import same_day_catchup
import schedule_control
import sheets_quota_runtime
import streaming_growth


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() == "true"


def _install_runtime() -> None:
    hardening.install()
    fit_scoring_hotfix.install()
    email_copy_intelligence.install()
    outreach_compliance.install()
    # Reliability installs its strict preflight and base delivery-window
    # wrapper first. Same-day catch-up must install afterwards so the bounded
    # recovery window remains the final active compliance gate.
    run_reliability.install()
    same_day_catchup.install()
    # Install after the compliance and strict-ledger wrappers so worksheet
    # handles and lead context are reused without weakening verification.
    sheets_quota_runtime.install()
    discovery_reliability.install()


def _reset_between_attempts() -> None:
    """Force every subsequent slot to re-read authoritative suppression state."""
    legacy._CONTACTED_CACHE = None
    legacy._LEDGER_CACHE = None
    legacy._BLOCKED_CACHE = None
    run_reliability._SENT_LEDGER_ROWS_CACHE = None
    run_reliability._DNC_ROWS_CACHE = None
    outreach_compliance._one_touch_cache = None
    outreach_compliance._one_touch_error = ""


def _run_claimed_attempt() -> None:
    attempt_id = os.environ.get("BDR_ATTEMPT_ID", "").strip()
    succeeded = False
    failure = ""
    worker_error: BaseException | None = None

    try:
        requested = max(1, int(os.environ.get("SEND_LIMIT", str(legacy.SEND_LIMIT))))
        effective = run_reliability.pacific_effective_send_limit(requested)
        if effective <= 0:
            print("[delivery-health] Pacific-day mailbox cap reached; no send attempted.")
            succeeded = True
            return
        os.environ["SEND_LIMIT"] = str(effective)
        streaming_growth.run(effective)
        succeeded = True
    except BaseException as exc:
        worker_error = exc
        failure = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if attempt_id:
            try:
                schedule_control.finish_slot(attempt_id, succeeded, failure)
            except Exception as finish_error:
                print(f"[schedule-control] could not finalize attempt: {finish_error}")
                if worker_error is None:
                    raise


def _claim_next_attempt(sequence: int, base_run_id: str) -> bool:
    _reset_between_attempts()
    # A single GitHub workflow can safely create multiple Run Control attempts
    # only when each has a deterministic distinct identifier.
    os.environ["GITHUB_RUN_ID"] = f"{base_run_id}-drain-{sequence}"
    result = auto_schedule.claim_due_slot(run_cap=8)
    if result.get("should_run") != "true":
        print(f"[same-day-drain] stopped: {result.get('reason', 'no_due_slot')}")
        return False
    os.environ["BDR_RUN_SLOT"] = result["run_slot"]
    os.environ["BDR_ATTEMPT_ID"] = result["attempt_id"]
    os.environ["SEND_LIMIT"] = result["send_limit"]
    print(
        f"[same-day-drain] claimed slot={result['run_slot']} "
        f"remaining_capacity={result['send_limit']} attempt={result['attempt_id']}"
    )
    return True


def main() -> None:
    _install_runtime()
    drain_enabled = _bool_env("DRAIN_ALL_ELAPSED_SLOTS", True)
    max_attempts = max(1, min(int(os.environ.get("MAX_DRAIN_ATTEMPTS", "8")), 12))
    base_run_id = os.environ.get("GITHUB_RUN_ID", "local")
    started = time.monotonic()
    # Keep the complete catch-up inside the existing 90-minute Actions timeout.
    worker_budget = max(600, min(int(os.environ.get("DRAIN_WORKER_BUDGET_SECONDS", "5100")), 5100))
    if drain_enabled:
        current_discovery = max(60, int(os.environ.get("DISCOVERY_BUDGET_SECONDS", "2700")))
        os.environ["DISCOVERY_BUDGET_SECONDS"] = str(min(current_discovery, 900))

    for sequence in range(1, max_attempts + 1):
        _run_claimed_attempt()
        if not drain_enabled:
            break
        if time.monotonic() - started >= worker_budget:
            print("[same-day-drain] worker budget reached; a later recovery heartbeat may continue.")
            break
        if not _claim_next_attempt(sequence, base_run_id):
            break


if __name__ == "__main__":
    main()
