"""Digest: Jobs — hiring signals via Signal Intelligence."""

import logging

from kronos.config import settings
from kronos.cron.notify import TOPIC_DIGEST_JOBS

log = logging.getLogger("kronos.cron.signal_jobs")


async def run_jobs_digest() -> None:
    """Generate the jobs digest in the dedicated Telegram topic."""
    if settings.agent_name != "kronos":
        return

    from kronos.signals.pipeline import run_signal_digest

    run = await run_signal_digest("jobs", topic_id=TOPIC_DIGEST_JOBS)
    log.info(
        "Signal jobs digest: %d items, %d clusters, sent=%s",
        run.saved_item_count,
        run.cluster_count,
        run.sent,
    )
