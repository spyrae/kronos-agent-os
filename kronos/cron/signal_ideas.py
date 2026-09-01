"""Digest: Product/Business Ideas — demand and pain-point signals."""

import logging

from kronos.config import settings
from kronos.cron.notify import TOPIC_DIGEST_IDEAS

log = logging.getLogger("kronos.cron.signal_ideas")


async def run_ideas_digest() -> None:
    """Generate the product/business ideas digest in the dedicated topic.

    Runs weekly, so it collects a week of signal, not a day (see WEEKLY_* in
    ``kronos.signals.pipeline``).
    """
    if settings.agent_name != "kronos":
        return

    from kronos.signals.pipeline import (
        WEEKLY_FETCH_LIMIT,
        WEEKLY_FRESHNESS,
        WEEKLY_LOOKBACK_HOURS,
        run_signal_digest,
    )

    run = await run_signal_digest(
        "ideas",
        topic_id=TOPIC_DIGEST_IDEAS,
        polish=True,
        curate=True,
        fetch_limit=WEEKLY_FETCH_LIMIT,
        freshness=WEEKLY_FRESHNESS,
        lookback_hours=WEEKLY_LOOKBACK_HOURS,
    )
    log.info(
        "Signal ideas digest: %d items, %d clusters, sent=%s",
        run.saved_item_count,
        run.cluster_count,
        run.sent,
    )
