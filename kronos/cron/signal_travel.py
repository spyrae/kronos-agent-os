"""Paused JB: Travel Insights — JourneyBay product opportunity signals."""

import logging

from kronos.config import settings

log = logging.getLogger("kronos.cron.signal_travel")
TRAVEL_INSIGHTS_ENABLED = False


async def run_travel_insights_digest() -> None:
    """Generate JourneyBay travel insights in the dedicated Telegram topic."""
    if not TRAVEL_INSIGHTS_ENABLED:
        log.info("Travel insights digest disabled; skipping collection/analysis/publication")
        return
    if settings.agent_name != "kronos":
        return

    from kronos.cron.notify import TOPIC_JB_TRAVEL_INSIGHTS
    from kronos.signals.pipeline import run_signal_digest

    run = await run_signal_digest("travel_insights", topic_id=TOPIC_JB_TRAVEL_INSIGHTS, polish=True)
    log.info(
        "Signal travel insights digest: %d items, %d clusters, sent=%s",
        run.saved_item_count,
        run.cluster_count,
        run.sent,
    )
