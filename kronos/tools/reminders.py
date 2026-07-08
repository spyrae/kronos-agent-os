"""schedule_task / schedule_followup tool family (roadmap 4.2 + 4.3).

The LLM converts the user's natural-language time to an absolute ISO timestamp;
the cron Scheduler later delivers to the chat the request came from (recovered
via the tool audit context). A "reminder" sends text; a "followup" re-runs the
agent on the promised task and sends the result.
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


def _resolve_target() -> tuple[int, str, int | None] | None:
    """(chat_id, thread_id, topic_id) for the current request, or None."""
    ctx = get_tool_audit_context()
    chat_raw = ctx.get("session_id", "")
    thread_id = ctx.get("thread_id", "")
    if not chat_raw:
        return None
    topic_id = None
    _, sep, tail = thread_id.partition(":")
    if sep and tail.isdigit():
        topic_id = int(tail)
    return int(chat_raw), thread_id, topic_id


def _parse_future(when_iso: str) -> tuple[float | None, str]:
    """Parse an ISO timestamp that must be in the future. (run_at, error_text)."""
    try:
        run_at = datetime.fromisoformat(when_iso).timestamp()
    except ValueError:
        return None, f"Неверный формат времени '{when_iso}'. Нужен ISO-8601, напр. 2026-07-11T18:00:00+08:00."
    if run_at <= datetime.now(UTC).timestamp():
        return None, "Это время уже прошло — укажи момент в будущем."
    return run_at, ""


@tool
def schedule_task(when_iso: str, message: str, repeat: str = "none") -> str:
    """Schedule a reminder — send this text to THIS chat at a later time.

    Convert the user's natural-language time to an absolute ISO-8601 timestamp
    (with timezone) yourself and pass it as when_iso.

    Args:
        when_iso: when to fire, ISO-8601 with timezone
            (e.g. "2026-07-11T18:00:00+08:00").
        message: the text to send to the chat when it fires.
        repeat: "none" (one-shot), "hourly", "daily", or "weekly".
    """
    target = _resolve_target()
    if target is None:
        return "Не удалось запланировать: неизвестен чат этого запроса."
    chat_id, thread_id, topic_id = target

    run_at, error = _parse_future(when_iso)
    if run_at is None:
        return error

    repeat = repeat.lower().strip()
    if repeat not in scheduled_tasks.RECUR_SECONDS:
        return f"repeat должен быть одним из: {', '.join(scheduled_tasks.RECUR_SECONDS)}."

    task_id = scheduled_tasks.add_task(
        agent_name=settings.agent_name,
        chat_id=chat_id,
        topic_id=topic_id,
        thread_id=thread_id,
        run_at=run_at,
        message=message,
        recur_seconds=scheduled_tasks.RECUR_SECONDS[repeat],
        kind="reminder",
    )
    tail_txt = "" if repeat == "none" else f", повтор: {repeat}"
    return f"✅ Запланировал (#{task_id}) на {when_iso}{tail_txt}."


@tool
def schedule_followup(when_iso: str, task: str) -> str:
    """Promise to come back later with a real result ("посмотрю позже").

    Use when you can't answer now but commit to follow up. When the time comes
    you (the agent) actually run `task` and send the user the result — not just
    a reminder. Convert natural-language time to ISO-8601 (with timezone).

    Args:
        when_iso: when to run the task, ISO-8601 with timezone.
        task: what to do then, phrased as an instruction to yourself
            (e.g. "check the DEV-1698 status and summarize the outcome").
    """
    target = _resolve_target()
    if target is None:
        return "Не удалось запланировать follow-up: неизвестен чат этого запроса."
    chat_id, thread_id, topic_id = target

    run_at, error = _parse_future(when_iso)
    if run_at is None:
        return error

    task_id = scheduled_tasks.add_task(
        agent_name=settings.agent_name,
        chat_id=chat_id,
        topic_id=topic_id,
        thread_id=thread_id,
        run_at=run_at,
        message=task,
        recur_seconds=0,
        kind="followup",
    )
    return f"🔔 Вернусь с результатом (#{task_id}) к {when_iso}."


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
        kind_txt = "↩︎ " if task.get("kind") == "followup" else ""
        lines.append(f"#{task['id']} @ {when}{recur_txt}: {kind_txt}{task['message'][:80]}")
    return "\n".join(lines)


@tool
def cancel_scheduled_task(task_id: int) -> str:
    """Cancel a pending scheduled task or follow-up by its id."""
    ok = scheduled_tasks.cancel_task(task_id, settings.agent_name)
    return f"Отменил задачу #{task_id}." if ok else f"Задача #{task_id} не найдена или уже неактивна."
