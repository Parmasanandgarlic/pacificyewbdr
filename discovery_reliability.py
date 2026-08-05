from __future__ import annotations

import os
from typing import Any, Callable

import requests

import bdr_agent as legacy

DEFAULT_APIFY_ACTOR = "compass/crawler-google-places"
_INSTALLED = False
_PRIOR_DISCOVER: Callable[[str], list[dict[str, Any]]] | None = None
_APIFY_CALLS = 0


def _actor_api_id(value: str) -> str:
    return value.strip().replace("/", "~")


def _normalized_places(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    places: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        website = str(item.get("website") or "").strip()
        title = str(item.get("title") or item.get("name") or "").strip()
        if not website or not title:
            continue
        places.append({
            "title": title,
            "website": website,
            "phone": str(item.get("phone") or item.get("phoneUnformatted") or "").strip(),
            "email": "",
        })
    return places


def discover_with_apify(query: str, actor: str, token: str) -> list[dict[str, Any]]:
    actor_id = _actor_api_id(actor)
    endpoint = (
        f"https://api.apify.com/v2/acts/{actor_id}/"
        f"run-sync-get-dataset-items?token={token}"
    )
    results_per_query = max(1, min(int(os.environ.get("RESULTS_PER_QUERY", "10")), 25))
    request_payload = {
        "searchStringsArray": [query],
        "maxCrawledPlacesPerSearch": results_per_query,
        "language": "en",
        "countryCode": "ca",
        "website": "withWebsite",
        "skipClosedPlaces": True,
        "scrapePlaceDetailPage": False,
        "scrapeContacts": False,
        "maxReviews": 0,
        "maxImages": 0,
    }
    response = requests.post(endpoint, json=request_payload, timeout=180)
    response.raise_for_status()
    return _normalized_places(response.json())


def reliable_discover_businesses(query: str) -> list[dict[str, Any]]:
    global _APIFY_CALLS
    assert _PRIOR_DISCOVER is not None

    token = (os.environ.get("APIFY_TOKEN") or "").strip()
    actor = (os.environ.get("APIFY_ACTOR") or DEFAULT_APIFY_ACTOR).strip()
    max_calls = max(0, min(int(os.environ.get("APIFY_MAX_CALLS_PER_RUN", "12")), 24))

    if token and actor and _APIFY_CALLS < max_calls:
        _APIFY_CALLS += 1
        try:
            places = discover_with_apify(query, actor, token)
            if places:
                print(
                    f"Discovered {len(places)} businesses via Apify "
                    f"actor={_actor_api_id(actor)} call={_APIFY_CALLS}/{max_calls}."
                )
                return places
            print("Apify returned no eligible business websites; using web-search fallback.")
        except Exception as exc:
            print(f"Apify discovery failed ({exc}); using web-search fallback.")

    return _PRIOR_DISCOVER(query)


def install() -> None:
    global _INSTALLED, _PRIOR_DISCOVER
    if _INSTALLED:
        return
    _PRIOR_DISCOVER = legacy.discover_businesses
    legacy.discover_businesses = reliable_discover_businesses
    _INSTALLED = True
