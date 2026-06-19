"""Deterministic reply-debt detector for Observer snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from kronos.observer.models import DialogSnapshot, ReplyDebt
from kronos.observer.state import ObserverState

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2}
NOISE_EXCERPTS = {"ok", "ок", "да", "нет", "👍", "👌", "+", "-", ".", "..."}


def detect_reply_debts(
    snapshots: list[DialogSnapshot] | tuple[DialogSnapshot, ...],
    now: datetime,
    threshold_hours: float = 8,
    *,
    state: ObserverState | None = None,
) -> list[ReplyDebt]:
    """Return deterministic reply debts sorted by severity and age."""
    debts: list[ReplyDebt] = []
    for snapshot in snapshots:
        if state and (snapshot.peer_id in state.ignored_peers or snapshot.peer_id in state.muted_peers):
            continue

        newest_relevant = _newest_relevant_message(snapshot)
        if not newest_relevant or newest_relevant.get("direction") != "incoming":
            continue

        excerpt = str(newest_relevant.get("excerpt") or snapshot.excerpt or "").strip()
        if _is_noise(excerpt):
            continue

        incoming_at = _parse_timestamp(newest_relevant.get("date") or snapshot.metadata.get("last_incoming_at"))
        if incoming_at is None:
            continue

        age_hours = (now - incoming_at).total_seconds() / 3600
        if age_hours <= threshold_hours:
            continue

        severity = _severity(age_hours)
        peer_title = snapshot.peer_title or snapshot.peer_id
        debts.append(
            ReplyDebt(
                peer_id=snapshot.peer_id,
                peer_title=peer_title,
                detected_at=_timestamp(now),
                last_incoming_at=_timestamp(incoming_at),
                last_incoming_message_id=_message_id(newest_relevant),
                last_incoming_excerpt=excerpt,
                hours_waiting=round(age_hours, 2),
                severity=severity,
                reason=f"last relevant message is incoming and {age_hours:.1f}h old",
                suggested_action=f"Ответить {peer_title}",
                confidence=0.9 if snapshot.unread_count > 0 else 0.75,
                metadata={
                    "unread_count": snapshot.unread_count,
                    "last_message_direction": newest_relevant.get("direction", ""),
                },
            )
        )

    return sorted(debts, key=lambda debt: (SEVERITY_RANK[debt.severity], -debt.hours_waiting, debt.peer_title))


def _newest_relevant_message(snapshot: DialogSnapshot) -> dict[str, Any] | None:
    messages = snapshot.metadata.get("recent_messages") or []
    for message in messages:
        if not isinstance(message, dict):
            continue
        excerpt = str(message.get("excerpt") or "").strip()
        if excerpt and not _is_noise(excerpt):
            return message

    direction = str(snapshot.metadata.get("last_message_direction") or "")
    if direction:
        return {
            "id": snapshot.last_message_id,
            "date": snapshot.metadata.get("last_incoming_at") if direction == "incoming" else "",
            "direction": direction,
            "excerpt": snapshot.excerpt,
        }
    return None


def _is_noise(excerpt: str) -> bool:
    normalized = " ".join((excerpt or "").casefold().split())
    if not normalized:
        return True
    if normalized in NOISE_EXCERPTS:
        return True
    return len(normalized) <= 4 and len(normalized.split()) <= 1


def _severity(age_hours: float) -> str:
    if age_hours >= 72:
        return "critical"
    if age_hours >= 24:
        return "high"
    return "medium"


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _message_id(message: dict[str, Any]) -> int | None:
    value = message.get("id")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
