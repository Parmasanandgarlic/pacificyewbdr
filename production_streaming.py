from __future__ import annotations

import os

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


def main() -> None:
    attempt_id = os.environ.get("BDR_ATTEMPT_ID", "").strip()
    succeeded = False
    failure = ""
    worker_error: BaseException | None = None

    try:
        hardening.install()
        fit_scoring_hotfix.install()
        email_copy_intelligence.install()
        outreach_compliance.install()
        same_day_catchup.install()
        run_reliability.install()
        # Install after the compliance and strict-ledger wrappers so worksheet
        # handles and lead context are reused without weakening verification.
        sheets_quota_runtime.install()
        discovery_reliability.install()
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


if __name__ == "__main__":
    main()
