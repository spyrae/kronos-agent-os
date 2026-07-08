"""Fire due user-scheduled tasks (roadmap 4.2).

Polled by the Scheduler each minute; every due task is delivered to its chat
via the self-webhook, then completed (one-shot → done, recurring → next run).
Delivery failures leave the task pending so the next cycle retries.
"""

import asyncio
import logging

from kronos import scheduled_tasks
from kronos.config import settings
from kronos.cron.notify import send_webhook

log = logging.getLogger("kronos.cron.reminders")


async def run_due_reminders() -> None:
    tasks = scheduled_tasks.due_tasks(settings.agent_name)
    if not tasks:
        return
    log.info("Firing %d due scheduled task(s)", len(tasks))
    for task in tasks:
        ok = await asyncio.to_thread(
            send_webhook,
            task["message"],
            task["chat_id"],
            None,  # parse_mode
            task["topic_id"],
        )
        if ok:
            scheduled_tasks.complete_task(task["id"], task["recur_seconds"], task["run_at"])
        else:
            log.warning("Reminder #%s delivery failed; will retry next cycle", task["id"])
