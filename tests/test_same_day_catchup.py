import os
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import same_day_catchup

PACIFIC = ZoneInfo("America/Vancouver")


class SameDayCatchupTests(unittest.TestCase):
    def test_standard_slot_window_remains_allowed(self):
        now = datetime(2026, 8, 6, 8, 13, tzinfo=PACIFIC)
        with patch.dict(
            os.environ,
            {
                "BDR_RUN_SLOT": "morning",
                "INITIAL_OUTREACH_ONLY": "true",
                "ALLOW_SAME_DAY_CATCHUP": "false",
            },
            clear=False,
        ):
            ok, reason = same_day_catchup.same_day_delivery_window_ok(now)
        self.assertTrue(ok, reason)

    def test_missed_morning_slot_can_recover_same_weekday(self):
        now = datetime(2026, 8, 6, 13, 45, tzinfo=PACIFIC)
        with patch.dict(
            os.environ,
            {
                "BDR_RUN_SLOT": "morning",
                "INITIAL_OUTREACH_ONLY": "true",
                "ALLOW_SAME_DAY_CATCHUP": "true",
                "SAME_DAY_CATCHUP_CUTOFF_MINUTES": "1020",
            },
            clear=False,
        ):
            ok, reason = same_day_catchup.same_day_delivery_window_ok(now)
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "same-day catch-up")

    def test_future_slot_cannot_run_early(self):
        now = datetime(2026, 8, 6, 13, 45, tzinfo=PACIFIC)
        with patch.dict(
            os.environ,
            {
                "BDR_RUN_SLOT": "afternoon",
                "INITIAL_OUTREACH_ONLY": "true",
                "ALLOW_SAME_DAY_CATCHUP": "true",
            },
            clear=False,
        ):
            ok, reason = same_day_catchup.same_day_delivery_window_ok(now)
        self.assertFalse(ok)
        self.assertIn("not due", reason)

    def test_catchup_stops_at_cutoff(self):
        now = datetime(2026, 8, 6, 17, 1, tzinfo=PACIFIC)
        with patch.dict(
            os.environ,
            {
                "BDR_RUN_SLOT": "morning",
                "INITIAL_OUTREACH_ONLY": "true",
                "ALLOW_SAME_DAY_CATCHUP": "true",
                "SAME_DAY_CATCHUP_CUTOFF_MINUTES": "1020",
            },
            clear=False,
        ):
            ok, reason = same_day_catchup.same_day_delivery_window_ok(now)
        self.assertFalse(ok)
        self.assertIn("cutoff", reason)

    def test_weekend_catchup_is_blocked(self):
        now = datetime(2026, 8, 8, 13, 45, tzinfo=PACIFIC)
        with patch.dict(
            os.environ,
            {
                "BDR_RUN_SLOT": "morning",
                "INITIAL_OUTREACH_ONLY": "true",
                "ALLOW_SAME_DAY_CATCHUP": "true",
            },
            clear=False,
        ):
            ok, reason = same_day_catchup.same_day_delivery_window_ok(now)
        self.assertFalse(ok)
        self.assertIn("weekdays", reason)

    def test_manual_delivery_remains_blocked(self):
        now = datetime(2026, 8, 6, 13, 45, tzinfo=PACIFIC)
        with patch.dict(
            os.environ,
            {
                "BDR_RUN_SLOT": "manual",
                "INITIAL_OUTREACH_ONLY": "true",
                "ALLOW_SAME_DAY_CATCHUP": "true",
            },
            clear=False,
        ):
            ok, reason = same_day_catchup.same_day_delivery_window_ok(now)
        self.assertFalse(ok)
        self.assertIn("manual delivery is disabled", reason)


if __name__ == "__main__":
    unittest.main()
