"""Fire due user-scheduled tasks (roadmap 4.2 + follow-ups 4.3).

Polled by the Scheduler each minute. A "reminder" task delivers its message
verbatim; a "followup" task runs the message as a prompt through the live agent
and delivers the agent's result. Both go to the originating chat via the
self-webhook; delivery/run failures leave the task pending so the next cycle
retries. One-shot → done, recurring → next run.
"""

import asyncio
import logging

from kronos import scheduled_tasks
from kronos.config import settings
from kronos.cron.notify import send_webhook

log = logging.getLogger("kronos.cron.reminders")


async def _run_followup(task: dict) -> str | None:
    """Run the promised work through the live agent; return its reply text."""
    from kronos.bridge import get_agent

    agent = get_agent()
    if agent is None:
        log.warning("Follow-up #%s skipped: agent not ready", task["id"])
        return None

    prompt = (
        "Отложенная задача, которую ты ранее пообещал выполнить: "
        f"{task['message']}\n\nВыполни её сейчас и дай результат пользователю."
    )
    try:
        return await agent.ainvoke(
            message=prompt,
            thread_id=task["thread_id"],
            user_id="scheduler",
            session_id=str(task["chat_id"]),
            source_kind="user",
            persist_user_turn=True,
        )
    except Exception as e:
        log.error("Follow-up #%s agent run failed: %s", task["id"], e)
        return None


async def _fire_task(task: dict) -> bool:
    if task.get("kind") == "followup":
        text = await _run_followup(task)
    else:
        text = task["message"]
    if not text:
        return False
    return await asyncio.to_thread(
        send_webhook, text, task["chat_id"], None, task["topic_id"]
    )


async def run_due_reminders() -> None:
    tasks = scheduled_tasks.due_tasks(settings.agent_name)
    if not tasks:
        return
    log.info("Firing %d due scheduled task(s)", len(tasks))
    for task in tasks:
        if await _fire_task(task):
            scheduled_tasks.complete_task(task["id"], task["recur_seconds"], task["run_at"])
        else:
            log.warning("Task #%s not delivered; will retry next cycle", task["id"])
