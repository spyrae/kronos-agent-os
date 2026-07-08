"""schedule_task tool family — durable reminders / recurring tasks (roadmap 4.2).

The LLM converts the user's natural-language time to an absolute ISO timestamp
and calls schedule_task; the cron Scheduler later delivers it to the chat the
request came from (recovered via the tool audit context).
"""

from datetime import UTC, datetime

from langchain_core.tools import tool

from kronos import scheduled_tasks
from kronos.audit import get_tool_audit_context
from kronos.config import settings


def _recur_name(recur_seconds: int) -> str:
    for name, seconds in scheduled_tasks.RECUR_SECONDS.items():
        if seconds == recur_seconds:
            return name
    return "custom"


@tool
def schedule_task(when_iso: str, message: str, repeat: str = "none") -> str:
    """Schedule a reminder or recurring task, delivered to THIS chat later.

    Convert the user's natural-language time to an absolute ISO-8601 timestamp
    (with timezone) yourself and pass it as when_iso.

    Args:
        when_iso: when to fire, ISO-8601 with timezone
            (e.g. "2026-07-11T18:00:00+08:00").
        message: the text to send to the chat when it fires.
        repeat: "none" (one-shot), "hourly", "daily", or "weekly".
    """
    ctx = get_tool_audit_context()
    chat_raw = ctx.get("session_id", "")
    thread_id = ctx.get("thread_id", "")
    if not chat_raw:
        return "Не удалось запланировать: неизвестен чат этого запроса."

    try:
        run_at = datetime.fromisoformat(when_iso).timestamp()
    except ValueError:
        return f"Неверный формат времени '{when_iso}'. Нужен ISO-8601, напр. 2026-07-11T18:00:00+08:00."

    if run_at <= datetime.now(UTC).timestamp():
        return "Это время уже прошло — укажи момент в будущем."

    repeat = repeat.lower().strip()
    if repeat not in scheduled_tasks.RECUR_SECONDS:
        return f"repeat должен быть одним из: {', '.join(scheduled_tasks.RECUR_SECONDS)}."

    topic_id = None
    _, sep, tail = thread_id.partition(":")
    if sep and tail.isdigit():
        topic_id = int(tail)

    task_id = scheduled_tasks.add_task(
        agent_name=settings.agent_name,
        chat_id=int(chat_raw),
        topic_id=topic_id,
        thread_id=thread_id,
        run_at=run_at,
        message=message,
        recur_seconds=scheduled_tasks.RECUR_SECONDS[repeat],
    )
    tail_txt = "" if repeat == "none" else f", повтор: {repeat}"
    return f"✅ Запланировал (#{task_id}) на {when_iso}{tail_txt}."


@tool
def list_scheduled_tasks() -> str:
    """List pending reminders / scheduled tasks for this agent."""
    pending = scheduled_tasks.list_pending(settings.agent_name)
    if not pending:
        return "Запланированных задач нет."
    lines = []
    for task in pending:
        when = datetime.fromtimestamp(task["run_at"], tz=UTC).isoformat()
        recur = _recur_name(task["recur_seconds"])
        recur_txt = "" if recur == "none" else f" ({recur})"
        lines.append(f"#{task['id']} @ {when}{recur_txt}: {task['message'][:80]}")
    return "\n".join(lines)


@tool
def cancel_scheduled_task(task_id: int) -> str:
    """Cancel a pending scheduled task by its id."""
    ok = scheduled_tasks.cancel_task(task_id, settings.agent_name)
    return f"Отменил задачу #{task_id}." if ok else f"Задача #{task_id} не найдена или уже неактивна."
