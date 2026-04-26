"""MONITOR node — collect current ASO data snapshot.

No LLM calls. Pure data collection from:
- App Store Connect (metadata, analytics)
- Google Play Developer API (metadata)
- iTunes Search API (keyword positions, competitors)
- Apple Search Ads (keyword popularity — optional)
"""

from __future__ import annotations

import logging
import uuid

from ..state import ASOState
from ..tools import app_store, keyword_tracker, competitor

log = logging.getLogger("aso.nodes.monitor")


async def monitor(state: ASOState) -> dict:
    """Collect all ASO data and return state updates."""
    log.info("=== MONITOR: starting data collection ===")

    cycle_id = str(uuid.uuid4())[:8]
    updates: dict = {
        "cycle_id": cycle_id,
        "phase": "monitor",
        "error": None,
    }

    # 1. iOS metadata from App Store Connect
    app_live = False
    try:
        metadata = await app_store.get_current_metadata()
        updates["metadata_ios"] = metadata
        if metadata:
            sample = next(iter(metadata.values()))
            version_state = sample.get("_version_state", "UNKNOWN")
            app_live = version_state == "READY_FOR_SALE"
            log.info("iOS metadata: %d locales (state: %s)", len(metadata), version_state)
        else:
            log.warning("No iOS metadata returned")
    except Exception as e:
        log.error("Failed to fetch iOS metadata: %s", e)
        updates["metadata_ios"] = {}
        updates["error"] = f"iOS metadata: {e}"

    # 2. Android metadata from Google Play Developer API
    try:
        from ..tools import play_store
        android_metadata = await play_store.get_current_metadata()
        updates["metadata_android"] = android_metadata
        log.info("Android metadata: %d locales", len(android_metadata))
    except Exception as e:
        log.warning("Play Store metadata skipped: %s", e)
        updates["metadata_android"] = {}

    # 3. Keyword research
    try:
        rankings = await keyword_tracker.track_all_keywords()
        updates["keyword_rankings"] = rankings
        found = sum(1 for r in rankings.values() if r.get("found"))
        total = len(rankings)
        if app_live:
            log.info("Keywords: %d tracked, %d found in top-50", total, found)
        else:
            log.info("Keywords: %d researched (pre-launch)", total)
    except Exception as e:
        log.error("Failed to track keywords: %s", e)
        updates["keyword_rankings"] = {}

    # 4. Apple Search Ads keyword popularity (optional, enriches keyword data)
    try:
        from ..tools import apple_search_ads
        all_keywords = [
            r.get("keyword", "") for r in (updates.get("keyword_rankings") or {}).values()
            if r.get("keyword")
        ]
        if all_keywords:
            popularity = await apple_search_ads.get_keyword_popularity(all_keywords)
            # Enrich rankings with popularity scores
            for key, data in updates.get("keyword_rankings", {}).items():
                kw = data.get("keyword", "")
                if kw in popularity:
                    data["popularity"] = popularity[kw].get("popularity")
                    data["popularity_rank"] = popularity[kw].get("rank")
            log.info("Enriched %d keywords with popularity scores", len(popularity))
    except Exception as e:
        log.debug("Apple Search Ads skipped (optional): %s", e)

    # 5. App info (ratings, reviews count)
    if app_live:
        try:
            app_info = await keyword_tracker.get_app_info()
            if app_info:
                updates["reviews_summary"] = {
                    "avg_rating": app_info.get("average_rating"),
                    "total_ratings": app_info.get("rating_count"),
                    "current_version_rating": app_info.get("current_version_rating"),
                    "current_version_ratings": app_info.get("current_version_rating_count"),
                }
        except Exception as e:
            log.warning("Failed to fetch app info: %s", e)
    else:
        updates["reviews_summary"] = {
            "avg_rating": None,
            "total_ratings": 0,
            "_note": "pre-launch",
        }

    # 6. Competitor data
    try:
        competitors = await competitor.get_competitor_info()
        updates["competitor_data"] = competitors
        log.info("Competitors: %d profiles", len(competitors))
    except Exception as e:
        log.warning("Failed to fetch competitors: %s", e)
        updates["competitor_data"] = []

    # 7. Analytics (only for live apps)
    if app_live:
        updates["analytics"] = state.get("analytics", {})
    else:
        updates["analytics"] = {"_note": "pre-launch"}

    log.info("=== MONITOR: complete (cycle %s, live=%s) ===", cycle_id, app_live)
    return updates
