import os
import unittest
from unittest.mock import Mock, patch

import discovery_reliability


class DiscoveryReliabilityTests(unittest.TestCase):
    def setUp(self):
        discovery_reliability._APIFY_CALLS = 0
        discovery_reliability._PRIOR_DISCOVER = lambda query: [
            {"title": f"Fallback {query}", "website": "https://fallback.example", "phone": "", "email": ""}
        ]

    def test_actor_owner_name_is_normalized_for_api_path(self):
        self.assertEqual(
            discovery_reliability._actor_api_id("compass/crawler-google-places"),
            "compass~crawler-google-places",
        )

    def test_apify_request_uses_current_official_input_schema(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = [
            {"title": "Clinic", "website": "https://clinic.ca", "phone": "+16045551234"}
        ]
        with patch.object(discovery_reliability.requests, "post", return_value=response) as post:
            results = discovery_reliability.discover_with_apify(
                "physiotherapy Vancouver",
                "compass/crawler-google-places",
                "token",
            )

        self.assertEqual(results[0]["website"], "https://clinic.ca")
        endpoint = post.call_args.args[0]
        payload = post.call_args.kwargs["json"]
        self.assertIn("compass~crawler-google-places", endpoint)
        self.assertEqual(payload["searchStringsArray"], ["physiotherapy Vancouver"])
        self.assertEqual(payload["website"], "withWebsite")
        self.assertFalse(payload["scrapeContacts"])

    def test_apify_failure_falls_back_to_free_discovery(self):
        with patch.dict(os.environ, {
            "APIFY_TOKEN": "token",
            "APIFY_ACTOR": "compass/crawler-google-places",
            "APIFY_MAX_CALLS_PER_RUN": "12",
        }, clear=False), patch.object(
            discovery_reliability,
            "discover_with_apify",
            side_effect=RuntimeError("provider unavailable"),
        ):
            results = discovery_reliability.reliable_discover_businesses("roofer Surrey")
        self.assertEqual(results[0]["title"], "Fallback roofer Surrey")

    def test_apify_calls_are_bounded_per_run(self):
        discovery_reliability._APIFY_CALLS = 1
        with patch.dict(os.environ, {
            "APIFY_TOKEN": "token",
            "APIFY_ACTOR": "compass/crawler-google-places",
            "APIFY_MAX_CALLS_PER_RUN": "1",
        }, clear=False), patch.object(discovery_reliability, "discover_with_apify") as apify:
            results = discovery_reliability.reliable_discover_businesses("dentist Burnaby")
        apify.assert_not_called()
        self.assertEqual(results[0]["title"], "Fallback dentist Burnaby")


if __name__ == "__main__":
    unittest.main()
