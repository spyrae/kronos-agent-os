"""Deterministic Daily Scope builder for private dialog snapshots."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from kronos.observer.models import DailyScopeEntry, DialogSnapshot
from kronos.security.pii import mask_pii
from kronos.workspace import Workspace, ws

LOCAL_TZ = timezone(timedelta(hours=8))
AGREEMENT_MARKERS = ("договорились", "жду", "скинь", "напомни", "давай", "сделаю")


def build_daily_scope(
    snapshots: list[DialogSnapshot] | tuple[DialogSnapshot, ...],
    now: datetime,
    *,
    local_tz: timezone = LOCAL_TZ,
    max_excerpts_per_peer: int = 3,
) -> list[DailyScopeEntry]:
    """Build deterministic per-contact daily scope entries from snapshots."""
    start, end = local_day_window(now, local_tz=local_tz)
    entries: list[DailyScopeEntry] = []

    for snapshot in snapshots:
        messages = _messages_for_day(snapshot, start=start, end=end)
        if not messages:
            continue
        excerpts = [_safe_excerpt(str(message.get("excerpt") or "")) for message in messages]
        excerpts = [excerpt for excerpt in excerpts if excerpt][:max_excerpts_per_peer]
        if not excerpts:
            continue

        agreements = [
            excerpt for excerpt in excerpts if any(marker in excerpt.casefold() for marker in AGREEMENT_MARKERS)
        ]
        last_direction = str(messages[0].get("direction") or "")
        risk = last_direction == "incoming"
        peer_title = snapshot.peer_title or snapshot.peer_id
        summary = "; ".join(excerpts)

        entries.append(
            DailyScopeEntry(
                peer_id=snapshot.peer_id,
                peer_title=peer_title,
                summary=summary,
                happened_at=start.date().isoformat(),
                excerpt=excerpts[0],
                topics=tuple(excerpts),
                metadata={
                    "agreements": agreements[:3],
                    "risk": risk,
                    "last_message_direction": last_direction,
                    "message_count": len(messages),
                    "suggested_action": "Проверить продолжение" if risk else "",
                },
            )
        )

    return entries


def local_day_window(now: datetime, *, local_tz: timezone = LOCAL_TZ) -> tuple[datetime, datetime]:
    """Return the current local-day window as UTC datetimes."""
    local_now = now.astimezone(local_tz)
    start_local = datetime.combine(local_now.date(), time.min, tzinfo=local_tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def save_daily_scope(
    entries: list[DailyScopeEntry] | tuple[DailyScopeEntry, ...],
    *,
    workspace: Workspace | None = None,
    day: datetime | None = None,
    body: str | None = None,
) -> Path:
    """Persist a Daily Scope markdown file under notes/user/daily-scope."""
    workspace = workspace or ws
    workspace.ensure_dirs()
    target_day = (day or datetime.now(UTC)).astimezone(LOCAL_TZ).date().isoformat()
    path = workspace.daily_scope_dir / f"{target_day}.md"
    content = body if body is not None else _markdown_daily_scope(entries, target_day)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    return path


def _messages_for_day(snapshot: DialogSnapshot, *, start: datetime, end: datetime) -> list[dict[str, Any]]:
    messages = snapshot.metadata.get("recent_messages") or []
    result: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        date = _parse_timestamp(message.get("date"))
        if date is None or not (start <= date < end):
            continue
        result.append(message)
    return result


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _safe_excerpt(value: str) -> str:
    return mask_pii(" ".join((value or "").split()))[:220]


def _markdown_daily_scope(entries: list[DailyScopeEntry] | tuple[DailyScopeEntry, ...], day: str) -> str:
    lines = [f"# Daily Scope — {day}", ""]
    if not entries:
        lines.append("Новых обсуждений в личке нет.")
        return "\n".join(lines)
    for entry in entries:
        agreements = entry.metadata.get("agreements") or []
        risk = bool(entry.metadata.get("risk"))
        lines.extend(
            [
                f"## {entry.peer_title or entry.peer_id}",
                f"- Обсуждали: {entry.summary}",
                f"- Договорённости: {'; '.join(agreements) if agreements else 'нет явных маркеров'}",
                f"- Риск: {'последнее сообщение входящее' if risk else 'без действий'}",
                "",
            ]
        )
    return "\n".join(lines)
