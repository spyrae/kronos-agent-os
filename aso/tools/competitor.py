"""Competitor analysis for ASO pipeline.

Uses iTunes Search API to find and compare competitor apps.
"""

from __future__ import annotations

import logging

from .keyword_tracker import get_app_info, search_itunes

log = logging.getLogger("aso.tools.competitor")

# Known competitors — extend via config
DEFAULT_COMPETITORS = [
    {"name": "TripIt", "bundle_id": "com.tripit.iphone"},
    {"name": "Wanderlog", "bundle_id": "com.wanderlog.ios"},
    {"name": "Sygic Travel", "bundle_id": "com.tripomatic.trip"},
    {"name": "TripAdvisor", "bundle_id": "com.tripadvisor.LocalPicks"},
]


async def get_competitor_info(competitors: list[dict] | None = None) -> list[dict]:
    """Fetch public info for competitor apps via iTunes Lookup API.

    Returns list of competitor data with ratings, descriptions, etc.
    """
    comps = competitors or DEFAULT_COMPETITORS
    results = []

    for comp in comps:
        try:
            info = await get_app_info(bundle_id=comp["bundle_id"])
            if info:
                info["competitor_name"] = comp["name"]
                info["bundle_id"] = comp["bundle_id"]
                results.append(info)
            else:
                log.warning("Competitor not found: %s (%s)", comp["name"], comp["bundle_id"])
        except Exception as e:
            log.warning("Failed to fetch competitor %s: %s", comp["name"], e)

    log.info("Fetched %d/%d competitor profiles", len(results), len(comps))
    return results


async def find_top_apps_for_keyword(keyword: str, *, country: str = "us", limit: int = 10) -> list[dict]:
    """Find top apps ranking for a specific keyword.

    Returns simplified list of top competitors for that term.
    """
    results = await search_itunes(keyword, country=country, limit=limit)

    return [
        {
            "position": i + 1,
            "name": app.get("trackName", ""),
            "bundle_id": app.get("bundleId", ""),
            "rating": app.get("averageUserRating"),
            "rating_count": app.get("userRatingCount", 0),
            "price": app.get("price", 0),
            "genre": app.get("primaryGenreName", ""),
        }
        for i, app in enumerate(results)
    ]
