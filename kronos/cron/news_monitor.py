"""News Monitor — Signal Intelligence news digest."""

import logging

from kronos.config import settings
from kronos.cron.notify import TOPIC_DIGEST_NEWS, send_bot_api

log = logging.getLogger("kronos.cron.news_monitor")


async def run_news_monitor() -> None:
    """Generate the unified Digest: News from X/Reddit/Telegram/search."""
    if settings.agent_name != "kronos":
        return

    from kronos.signals.pipeline import run_signal_digest

    run = await run_signal_digest("news", topic_id=TOPIC_DIGEST_NEWS)
    if not run.sent and run.saved_item_count == 0:
        log.info("No signal news items collected; sending compatibility empty digest notice")
        send_bot_api(run.rendered.body, parse_mode="HTML", topic_id=TOPIC_DIGEST_NEWS)
    log.info(
        "Signal news digest: %d items, %d clusters, sent=%s",
        run.saved_item_count,
        run.cluster_count,
        run.sent,
    )
