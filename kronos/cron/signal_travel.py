"""JB: Travel Insights — JourneyBay product opportunity signals."""

import logging

from kronos.config import settings
from kronos.cron.notify import TOPIC_JB_TRAVEL_INSIGHTS

log = logging.getLogger("kronos.cron.signal_travel")


async def run_travel_insights_digest() -> None:
    """Generate JourneyBay travel insights in the dedicated Telegram topic."""
    if settings.agent_name != "kronos":
        return

    from kronos.signals.pipeline import run_signal_digest

    run = await run_signal_digest("travel_insights", topic_id=TOPIC_JB_TRAVEL_INSIGHTS, polish=True)
    log.info(
        "Signal travel insights digest: %d items, %d clusters, sent=%s",
        run.saved_item_count,
        run.cluster_count,
        run.sent,
    )
