"""Delivery keeps moving independently of model calls and plan polling."""

import asyncio
import logging

from kronos import plans
from kronos.session import SessionStore

log = logging.getLogger("kronos.cron.delivery")


async def _run_producer(producer) -> None:
    while True:
        try:
            await producer()
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is None or task.cancelling():
                raise
            # A transport may raise CancelledError without cancellation of its
            # worker. Do not silently lose one TaskGroup producer in that case.
            log.warning("Delivery producer interrupted; duty retained")
        except Exception as error:
            log.error("Delivery cycle failed (%s)", type(error).__name__)
        await asyncio.sleep(5)


async def run_delivery_worker(session_store: SessionStore | None = None) -> None:
    """Each producer keeps moving even while another awaits transport timeout."""
    if session_store is None:
        await _run_producer(plans.deliver_pending)
        return
    async with asyncio.TaskGroup() as group:
        group.create_task(_run_producer(plans.deliver_pending))
        group.create_task(_run_producer(session_store.deliver_pending))
