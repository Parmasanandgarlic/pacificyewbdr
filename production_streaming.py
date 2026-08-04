from __future__ import annotations

import os

import bdr_agent as legacy
import production_hardening as hardening
import streaming_growth


def main() -> None:
    hardening.install()
    requested = max(1, int(os.environ.get("SEND_LIMIT", str(legacy.SEND_LIMIT))))
    effective = hardening.effective_send_limit(requested)
    if effective <= 0:
        print("[delivery-health] Pacific-day mailbox cap reached; no send attempted.")
        return
    os.environ["SEND_LIMIT"] = str(effective)
    streaming_growth.run(effective)


if __name__ == "__main__":
    main()
