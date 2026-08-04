# BDR Production Hardening

The production runner is `production_hardening.py`.

It wraps the growth engine with:

- Sheet preflight based on required columns rather than brittle exact header order.
- Explicit opt-out parsing limited to the recipient's newly written reply text.
- Permanent 5.x DSN suppression without treating temporary 4.x delays as hard bounces.
- Protected-role inbox filtering for privacy, legal, careers, security, and similar addresses.
- Provider-configured SMTP, Message-ID, Reply-To, and List-Unsubscribe headers.
- Twelve-to-twenty-second randomized send pacing.
- A 48-message UTC daily mailbox ceiling across scheduled and manual runs.
- Manual query slices that avoid all four current-day scheduled slices.
- Reservation timestamps for send reconciliation.
- Complete compile and unit-test coverage in the production quality gate.

Scheduled and immediate workflows both execute the same hardened runner.
