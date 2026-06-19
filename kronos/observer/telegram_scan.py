"""Read-only Telegram dialog scanner for Observer digests."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from kronos.observer.models import DialogSnapshot, ObserverRunResult, ObserverSourceKind, utc_now_iso
from kronos.observer.state import ObserverState, ObserverStateStore
from kronos.security.pii import mask_pii

SCAN_NAME = "telegram_private_dialogs"
MAX_EXCERPT_CHARS = 220


async def scan_private_dialogs(
    client,
    state: ObserverStateStore | ObserverState,
    *,
    limit_dialogs: int,
    limit_messages_per_dialog: int,
    since: datetime | None = None,
    dry_run: bool = False,
    unread_only: bool = True,
) -> list[DialogSnapshot]:
    """Collect safe snapshots for unread private Telegram dialogs.

    This scanner is read-only with respect to Telegram: it only calls
    ``iter_dialogs`` and ``iter_messages`` on the provided client.
    """
    store = state if isinstance(state, ObserverStateStore) else None
    observer_state = store.load() if store else state
    snapshots: list[DialogSnapshot] = []
    scanned_count = 0
    skipped_count = 0

    async for dialog in client.iter_dialogs(limit=limit_dialogs):
        scanned_count += 1
        if not _is_scannable_private_dialog(dialog):
            skipped_count += 1
            continue

        peer_id = _peer_id(dialog)
        if not peer_id or peer_id in observer_state.ignored_peers or peer_id in observer_state.muted_peers:
            skipped_count += 1
            continue

        unread_count = int(getattr(dialog, "unread_count", 0) or 0)
        if unread_only and unread_count <= 0:
            skipped_count += 1
            continue

        messages = await _read_recent_messages(
            client,
            getattr(dialog, "entity", dialog),
            limit=limit_messages_per_dialog,
            since=since,
        )
        if not messages:
            skipped_count += 1
            continue

        snapshot = _build_snapshot(dialog, peer_id=peer_id, unread_count=unread_count, messages=messages)
        snapshots.append(snapshot)

    if not dry_run:
        _update_state(observer_state, snapshots)
        finished_at = utc_now_iso()
        observer_state.last_scan_at[SCAN_NAME] = finished_at
        if store:
            store.save(observer_state)
            store.append_run(
                ObserverRunResult(
                    source_kind=ObserverSourceKind.TELEGRAM_UNREAD_DIGEST,
                    run_id=f"{SCAN_NAME}:{finished_at}",
                    status="completed",
                    finished_at=finished_at,
                    scanned_count=scanned_count,
                    captured_count=len(snapshots),
                    skipped_count=skipped_count,
                )
            )

    return snapshots


def _is_scannable_private_dialog(dialog) -> bool:
    entity = getattr(dialog, "entity", None)
    if getattr(dialog, "is_group", False) or getattr(dialog, "is_channel", False):
        return False
    if entity is not None and (
        getattr(entity, "bot", False)
        or getattr(entity, "is_self", False)
        or getattr(entity, "megagroup", False)
        or getattr(entity, "broadcast", False)
    ):
        return False
    if hasattr(dialog, "is_user"):
        return bool(getattr(dialog, "is_user"))
    return entity is not None


async def _read_recent_messages(
    client,
    entity,
    *,
    limit: int,
    since: datetime | None,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    async for msg in client.iter_messages(entity, limit=limit):
        date = getattr(msg, "date", None)
        if since is not None and date is not None and date < since:
            break
        text = _compact_text(_message_text(msg))
        direction = "outgoing" if bool(getattr(msg, "out", False)) else "incoming"
        messages.append(
            {
                "id": int(getattr(msg, "id", 0) or 0),
                "date": _timestamp(date),
                "direction": direction,
                "excerpt": _safe_excerpt(text),
                "has_text": bool(text),
            }
        )
    return messages


def _build_snapshot(
    dialog,
    *,
    peer_id: str,
    unread_count: int,
    messages: list[dict[str, Any]],
) -> DialogSnapshot:
    entity = getattr(dialog, "entity", None)
    incoming = [msg for msg in messages if msg["direction"] == "incoming"]
    outgoing = [msg for msg in messages if msg["direction"] == "outgoing"]
    last_message = messages[0]
    excerpt = _first_nonempty_excerpt(incoming) or _first_nonempty_excerpt(messages)
    last_incoming_at = incoming[0]["date"] if incoming else ""
    last_outgoing_at = outgoing[0]["date"] if outgoing else ""
    last_message_direction = str(last_message["direction"])

    return DialogSnapshot(
        peer_id=peer_id,
        peer_title=_peer_title(entity, dialog),
        last_message_id=int(last_message["id"] or 0) or None,
        unread_count=unread_count,
        message_count=len(messages),
        summary=f"{unread_count} unread; last message is {last_message_direction}",
        excerpt=excerpt,
        metadata={
            "username": str(getattr(entity, "username", "") or ""),
            "last_incoming_at": last_incoming_at,
            "last_outgoing_at": last_outgoing_at,
            "last_message_direction": last_message_direction,
            "recent_messages": messages,
        },
    )


def _update_state(state: ObserverState, snapshots: list[DialogSnapshot]) -> None:
    for snapshot in snapshots:
        if snapshot.last_message_id is not None:
            state.last_seen_message_ids[snapshot.peer_id] = int(snapshot.last_message_id)
            state.dialog_cursors[snapshot.peer_id] = f"msg:{snapshot.last_message_id}"


def _peer_id(dialog) -> str:
    entity = getattr(dialog, "entity", None)
    value = getattr(entity, "id", None) or getattr(dialog, "id", None)
    return str(value or "")


def _peer_title(entity, dialog) -> str:
    first = str(getattr(entity, "first_name", "") or "").strip()
    last = str(getattr(entity, "last_name", "") or "").strip()
    full_name = " ".join(part for part in (first, last) if part)
    return (
        full_name
        or str(getattr(entity, "username", "") or "")
        or str(getattr(entity, "title", "") or "")
        or str(getattr(dialog, "name", "") or "")
    )


def _message_text(msg) -> str:
    return str(
        getattr(msg, "message", None)
        or getattr(msg, "text", None)
        or getattr(msg, "raw_text", None)
        or ""
    )


def _compact_text(text: str) -> str:
    return " ".join((text or "").split())


def _safe_excerpt(text: str) -> str:
    return mask_pii(_compact_text(text))[:MAX_EXCERPT_CHARS]


def _first_nonempty_excerpt(messages: list[dict[str, Any]]) -> str:
    for msg in messages:
        if msg["excerpt"]:
            return str(msg["excerpt"])
    return ""


def _timestamp(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value or "")
