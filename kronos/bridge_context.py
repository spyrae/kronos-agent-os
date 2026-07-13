"""Transient conversation-context formatting for the bridge.

Synchronous text helpers extracted from ``bridge.py``: clipping, the shared
swarm-ledger context block, and message timestamp rendering. The swarm store
is passed in as an argument, so there is no live module state here.
"""

import logging

from kronos.config import settings

# Logger name kept as "kronos.bridge" so extracted log lines are unchanged.
log = logging.getLogger("kronos.bridge")


def _clip_context_text(text: str, limit: int = 500) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)].rstrip() + "..."


def _format_shared_group_context(
    swarm,
    *,
    chat_id: int,
    topic_id: int | None,
    current_msg_id: int | None,
) -> str:
    """Build transient context from the shared swarm ledger."""
    limit = max(0, min(settings.telegram_shared_context_messages, 30))
    if limit <= 0:
        return ""

    try:
        rows = swarm.get_recent_messages(chat_id=chat_id, topic_id=topic_id, limit=limit + 1)
    except Exception as e:
        log.warning("[Swarm] Failed to load shared topic context: %s", e)
        return ""

    rows = [row for row in rows if row.get("msg_id") != current_msg_id]
    if not rows:
        return ""

    lines: list[str] = []
    for row in reversed(rows[:limit]):
        sender_type = row.get("sender_type")
        if sender_type == "agent":
            who = f"Агент {row.get('agent_name') or 'unknown'}"
        elif sender_type == "system":
            who = "Система"
        else:
            who = "Пользователь"
        lines.append(f"- {who}: {_clip_context_text(str(row.get('text') or ''))}")

    if not lines:
        return ""
    return (
        "[Общая история этого Telegram-топика]\n"
        "Ниже недавние сообщения из общего журнала. Используй их как контекст, "
        "но не считай новым запросом и не пересказывай без необходимости.\n" + "\n".join(lines)
    )


def _message_timestamp(event) -> str:
    value = getattr(getattr(event, "message", None), "date", None)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value or "")
