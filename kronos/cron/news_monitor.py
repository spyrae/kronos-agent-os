"""News Monitor — Signal Intelligence news digest."""

import logging

from kronos.config import settings
from kronos.cron.notify import TOPIC_DIGEST_NEWS, send_bot_api

log = logging.getLogger("kronos.cron.news_monitor")


async def run_news_monitor() -> None:
    """Generate the unified Digest: News from X/Reddit/Telegram/search.

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
        "news",
        topic_id=TOPIC_DIGEST_NEWS,
        polish=True,
        curate=True,
        fetch_limit=WEEKLY_FETCH_LIMIT,
        freshness=WEEKLY_FRESHNESS,
        lookback_hours=WEEKLY_LOOKBACK_HOURS,
    )
    if not run.sent and run.saved_item_count == 0:
        log.info("No signal news items collected; sending compatibility empty digest notice")
        send_bot_api(run.rendered.body, parse_mode="HTML", topic_id=TOPIC_DIGEST_NEWS)
    log.info(
        "Signal news digest: %d items, %d clusters, sent=%s",
        run.saved_item_count,
        run.cluster_count,
        run.sent,
    )
