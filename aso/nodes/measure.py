"""MEASURE node — collect post-change metrics.

Runs after the wait period. Collects the same data types
as the baseline to enable delta comparison.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..state import ASOState
from ..tools import app_store, keyword_tracker

log = logging.getLogger("aso.nodes.measure")


async def measure(state: ASOState) -> dict:
    """Collect post-change metrics for comparison with baseline."""
    log.info("=== MEASURE: collecting post-change metrics ===")

    post_metrics: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "keyword_positions": {},
        "ratings": {},
    }

    # 1. Re-track keyword positions
    try:
        rankings = await keyword_tracker.track_all_keywords()
        for key, data in rankings.items():
            if data.get("position"):
                post_metrics["keyword_positions"][data["keyword"]] = data["position"]
        found = sum(1 for r in rankings.values() if r.get("found"))
        log.info("Post-change keywords: %d tracked, %d found", len(rankings), found)
        # Also update state rankings for future cycles
        post_metrics["_keyword_rankings_full"] = rankings
    except Exception as e:
        log.error("Failed to track keywords: %s", e)

    # 2. App info (ratings, reviews)
    try:
        app_info = await keyword_tracker.get_app_info()
        if app_info:
            post_metrics["ratings"] = {
                "avg_rating": app_info.get("average_rating"),
                "total_ratings": app_info.get("rating_count"),
            }
    except Exception as e:
        log.warning("Failed to fetch app info: %s", e)

    # 3. Analytics (if available — app must be live)
    try:
        live = await app_store.is_live()
        if live:
            # Request fresh analytics
            request_id = await app_store.request_analytics_report()
            post_metrics["analytics_request_id"] = request_id
            log.info("Analytics report requested: %s", request_id)
        else:
            log.info("App not live — skipping analytics collection")
    except Exception as e:
        log.warning("Analytics request failed: %s", e)

    log.info("=== MEASURE: complete ===")

    return {
        "phase": "measure",
        "post_metrics": post_metrics,
        # Update keyword rankings with fresh data
        "keyword_rankings": post_metrics.get("_keyword_rankings_full", state.get("keyword_rankings", {})),
    }
