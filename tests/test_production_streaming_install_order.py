import os
import unittest
from unittest.mock import patch

import production_streaming


class ProductionStreamingInstallOrderTests(unittest.TestCase):
    def test_same_day_catchup_is_installed_after_reliability_window(self):
        order = []
        installers = [
            (production_streaming.hardening, "hardening"),
            (production_streaming.fit_scoring_hotfix, "fit_scoring"),
            (production_streaming.email_copy_intelligence, "copy"),
            (production_streaming.outreach_compliance, "compliance"),
            (production_streaming.run_reliability, "reliability"),
            (production_streaming.same_day_catchup, "catchup"),
            (production_streaming.sheets_quota_runtime, "sheets"),
            (production_streaming.discovery_reliability, "discovery"),
        ]
        patches = [
            patch.object(module, "install", side_effect=lambda name=name: order.append(name))
            for module, name in installers
        ]
        with patch.dict(
            os.environ,
            {
                "BDR_ATTEMPT_ID": "",
                "SEND_LIMIT": "8",
                "DRAIN_ALL_ELAPSED_SLOTS": "false",
            },
            clear=False,
        ), patch.object(
            production_streaming.run_reliability,
            "pacific_effective_send_limit",
            return_value=0,
        ):
            for mocked in patches:
                mocked.start()
            try:
                production_streaming.main()
            finally:
                for mocked in reversed(patches):
                    mocked.stop()

        self.assertLess(order.index("reliability"), order.index("catchup"))
        self.assertEqual(order[-2:], ["sheets", "discovery"])


if __name__ == "__main__":
    unittest.main()
