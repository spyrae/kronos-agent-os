"""Delivery keeps moving independently of model calls and plan polling."""

import asyncio
import logging

from kronos import plans

log = logging.getLogger("kronos.cron.delivery")


async def run_delivery_worker() -> None:
    """Drain bounded durable work until the application cancels the service."""
    while True:
        try:
            await plans.deliver_pending()
        except Exception as error:
            # Do not let a transient DB error silently kill the delivery loop;
            # the pending duty remains in SQLite. Never log payload/credentials.
            log.error("Delivery cycle failed (%s)", type(error).__name__)
        await asyncio.sleep(5)
