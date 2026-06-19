"""Telegram-safe renderers for Observer digests."""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from kronos.observer.models import DialogSnapshot, ReplyDebt


def render_morning_observer_digest(
    snapshots: list[DialogSnapshot] | tuple[DialogSnapshot, ...],
    debts: list[ReplyDebt] | tuple[ReplyDebt, ...],
    *,
    generated_at: datetime | None = None,
    max_unread: int = 8,
    max_debts: int = 8,
) -> str:
    """Render the morning Observer digest as Telegram HTML."""
    generated = generated_at or datetime.now(UTC)
    lines = [
        "<b>🌅 Утренний обзор лички</b>",
        f"<i>{escape(generated.strftime('%Y-%m-%d %H:%M UTC'))}</i>",
        "",
    ]

    if not snapshots and not debts:
        lines.append("Новых долгов/непрочитанного нет.")
        return "\n".join(lines)

    if snapshots:
        lines.append("<b>Непрочитанное:</b>")
        for index, snapshot in enumerate(snapshots[:max_unread], start=1):
            title = escape(snapshot.peer_title or snapshot.peer_id)
            excerpt = escape(_compact(snapshot.excerpt) or "без текстового фрагмента")
            lines.append(f"{index}. <b>{title}</b> — {snapshot.unread_count} сообщ., главное: {excerpt}")
        if len(snapshots) > max_unread:
            lines.append(f"…и ещё {len(snapshots) - max_unread}")
        lines.append("")

    if debts:
        lines.append("<b>Ждут ответа:</b>")
        for debt in debts[:max_debts]:
            title = escape(debt.peer_title or debt.peer_id)
            excerpt = escape(_compact(debt.last_incoming_excerpt) or "без текстового фрагмента")
            age = _age_label(debt.hours_waiting)
            severity = escape(debt.severity)
            lines.append(f"• <b>{title}</b> — {age}, {severity}; последний входящий: {excerpt}")
        if len(debts) > max_debts:
            lines.append(f"…и ещё {len(debts) - max_debts}")

    return "\n".join(line for line in lines if line != "__drop__").strip()


def _compact(text: str) -> str:
    return " ".join((text or "").split())[:240]


def _age_label(hours: float) -> str:
    if hours >= 48:
        return f"{round(hours / 24)}д"
    if hours >= 24:
        return "1д"
    return f"{round(hours)}ч"
