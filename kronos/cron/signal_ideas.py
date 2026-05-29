"""Digest: Product/Business Ideas — demand and pain-point signals."""

import logging

from kronos.config import settings
from kronos.cron.notify import TOPIC_DIGEST_IDEAS

log = logging.getLogger("kronos.cron.signal_ideas")


async def run_ideas_digest() -> None:
    """Generate the product/business ideas digest in the dedicated topic."""
    if settings.agent_name != "kronos":
        return

    from kronos.signals.pipeline import run_signal_digest

    run = await run_signal_digest("ideas", topic_id=TOPIC_DIGEST_IDEAS, polish=True)
    log.info(
        "Signal ideas digest: %d items, %d clusters, sent=%s",
        run.saved_item_count,
        run.cluster_count,
        run.sent,
    )
