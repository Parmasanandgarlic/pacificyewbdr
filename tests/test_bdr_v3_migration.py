from __future__ import annotations

import unittest
from pathlib import Path


class MigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(Path("migrations").glob("*_bdr_v3_*.sql"))
        ).lower()

    def test_required_operating_tables_exist(self):
        for table in (
            "bdr_accounts",
            "bdr_contacts",
            "bdr_evidence",
            "bdr_mailboxes",
            "bdr_campaigns",
            "bdr_enrollments",
            "bdr_touches",
            "bdr_messages",
            "bdr_suppressions",
            "bdr_replies",
            "bdr_opportunities",
            "bdr_outcomes",
            "bdr_audit_events",
        ):
            self.assertIn(f"create table if not exists {table}", self.sql)

    def test_transactional_functions_exist(self):
        for function in (
            "claim_bdr_touches",
            "get_bdr_dispatch_context",
            "reserve_bdr_message",
            "complete_bdr_message",
            "fail_bdr_message",
            "stop_bdr_enrollment",
        ):
            self.assertIn(f"function {function}", self.sql)

    def test_security_definer_functions_are_not_public(self):
        self.assertIn("revoke all on function claim_bdr_touches", self.sql)
        self.assertIn("grant execute on function claim_bdr_touches", self.sql)
        self.assertIn("to service_role", self.sql)

    def test_claim_only_selects_approved_required_touches(self):
        self.assertIn("not s.requires_approval or t.approved_at is not null", self.sql)

    def test_messages_and_touches_have_unique_idempotency(self):
        self.assertGreaterEqual(
            self.sql.count("idempotency_key text not null unique"),
            2,
        )


if __name__ == "__main__":
    unittest.main()
