"""Manual Observer controls used by Telegram DM commands and helpers."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kronos.config import settings
from kronos.observer.models import (
    DialogSnapshot,
    ObserverRunResult,
    ObserverSourceKind,
    ReplyDebt,
    utc_now_iso,
)
from kronos.observer.render import render_morning_observer_digest
from kronos.observer.reply_debts import detect_reply_debts
from kronos.observer.state import ObserverStateStore
from kronos.observer.telegram_scan import scan_private_dialogs
from kronos.security.pii import mask_pii

DEFAULT_LIMIT_DIALOGS = int(os.environ.get("OBSERVER_MAX_DIALOGS") or "50")
DEFAULT_LIMIT_MESSAGES = int(os.environ.get("OBSERVER_MAX_MESSAGES_PER_DIALOG") or "20")
DEFAULT_REPLY_THRESHOLD_HOURS = float(os.environ.get("OBSERVER_REPLY_THRESHOLD_HOURS") or "8")
MANUAL_SOURCE_KIND = ObserverSourceKind.OBSERVER_MANUAL_COMMAND


@dataclass(frozen=True)
class ObserverStatus:
    """Safe status summary for manual Observer controls."""

    generated_at: str
    enabled_jobs: dict[str, bool]
    last_scan_at: dict[str, str]
    last_digest_at: dict[str, str]
    ignored_peers: tuple[str, ...]
    muted_peers: tuple[str, ...]
    ignored_peer_reasons: dict[str, str]
    muted_peer_reasons: dict[str, str]
    recent_runs: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ObserverDigestRun:
    """Result of a manual morning digest run."""

    body: str
    snapshots: tuple[DialogSnapshot, ...]
    debts: tuple[ReplyDebt, ...]
    dry_run: bool
    sent: bool = False
    skipped_reason: str = ""


@dataclass(frozen=True)
class ObserverDebtsRun:
    """Result of a manual reply-debts scan."""

    debts: tuple[ReplyDebt, ...]
    skipped_reason: str = ""


def ignore_peer(
    peer_id: str,
    reason: str = "",
    *,
    state_store: ObserverStateStore | None = None,
    actor_id: str = "",
) -> str:
    """Add a peer to the Observer ignore list and audit the command."""
    store = state_store or ObserverStateStore()
    peer = _normalize_peer(peer_id)
    clean_reason = _clean_reason(reason)
    store.set_ignored(peer, True, reason=clean_reason)
    _append_manual_command(
        store,
        "ignore_peer",
        actor_id=actor_id,
        peer_id=peer,
        metadata={"reason": clean_reason},
    )
    return peer


def unignore_peer(
    peer_id: str,
    *,
    state_store: ObserverStateStore | None = None,
    actor_id: str = "",
) -> str:
    """Remove a peer from the Observer ignore list and audit the command."""
    store = state_store or ObserverStateStore()
    peer = _normalize_peer(peer_id)
    store.set_ignored(peer, False)
    _append_manual_command(store, "unignore_peer", actor_id=actor_id, peer_id=peer)
    return peer


def mute_peer(
    peer_id: str,
    reason: str = "",
    *,
    state_store: ObserverStateStore | None = None,
    actor_id: str = "",
) -> str:
    """Add a peer to the Observer mute list and audit the command."""
    store = state_store or ObserverStateStore()
    peer = _normalize_peer(peer_id)
    clean_reason = _clean_reason(reason)
    store.set_muted(peer, True, reason=clean_reason)
    _append_manual_command(
        store,
        "mute_peer",
        actor_id=actor_id,
        peer_id=peer,
        metadata={"reason": clean_reason},
    )
    return peer


def unmute_peer(
    peer_id: str,
    *,
    state_store: ObserverStateStore | None = None,
    actor_id: str = "",
) -> str:
    """Remove a peer from the Observer mute list and audit the command."""
    store = state_store or ObserverStateStore()
    peer = _normalize_peer(peer_id)
    store.set_muted(peer, False)
    _append_manual_command(store, "unmute_peer", actor_id=actor_id, peer_id=peer)
    return peer


def observer_status(
    *,
    state_store: ObserverStateStore | None = None,
    recent_run_limit: int = 5,
    enabled_jobs: Mapping[str, bool] | None = None,
) -> ObserverStatus:
    """Return a status summary without message bodies or run metadata."""
    store = state_store or ObserverStateStore()
    state = store.load()
    jobs = dict(
        enabled_jobs
        or {
            "personal-observer": settings.agent_name == "kronos",
            "daily-scope": settings.agent_name == "kronos",
        }
    )
    return ObserverStatus(
        generated_at=utc_now_iso(),
        enabled_jobs=jobs,
        last_scan_at=dict(sorted(state.last_scan_at.items())),
        last_digest_at=dict(sorted(state.last_digest_at.items())),
        ignored_peers=tuple(sorted(state.ignored_peers)),
        muted_peers=tuple(sorted(state.muted_peers)),
        ignored_peer_reasons=dict(sorted(state.ignored_peer_reasons.items())),
        muted_peer_reasons=dict(sorted(state.muted_peer_reasons.items())),
        recent_runs=tuple(_safe_run_summary(run) for run in store.list_runs(limit=recent_run_limit)),
    )


def render_observer_status(status: ObserverStatus) -> str:
    """Render Observer status as a privacy-safe Telegram/plain-text response."""
    lines = [
        "Observer status",
        f"Generated: {status.generated_at}",
        "",
        "Jobs:",
    ]
    for name, enabled in sorted(status.enabled_jobs.items()):
        lines.append(f"- {name}: {'enabled' if enabled else 'disabled'}")

    lines.extend(["", "Last scans:"])
    lines.extend(_format_mapping(status.last_scan_at))
    lines.extend(["", "Last digests:"])
    lines.extend(_format_mapping(status.last_digest_at))
    lines.extend(["", f"Ignored peers ({len(status.ignored_peers)}):"])
    lines.extend(_format_peer_list(status.ignored_peers, status.ignored_peer_reasons))
    lines.extend(["", f"Muted peers ({len(status.muted_peers)}):"])
    lines.extend(_format_peer_list(status.muted_peers, status.muted_peer_reasons))

    lines.extend(["", "Recent runs:"])
    if not status.recent_runs:
        lines.append("- none")
    for run in status.recent_runs:
        counters = (
            f"scanned={run['scanned_count']} captured={run['captured_count']} "
            f"skipped={run['skipped_count']} errors={run['error_count']}"
        )
        lines.append(f"- {run['source_kind']} {run['status']} at {run['logged_at']} ({counters})")
    return "\n".join(lines).strip()


async def run_morning_digest(
    *,
    client=None,
    state_store: ObserverStateStore | None = None,
    scanner=scan_private_dialogs,
    detector=detect_reply_debts,
    renderer=render_morning_observer_digest,
    sender: Callable[..., bool] | None = None,
    now: datetime | None = None,
    dry_run: bool = True,
    actor_id: str = "",
    limit_dialogs: int = DEFAULT_LIMIT_DIALOGS,
    limit_messages_per_dialog: int = DEFAULT_LIMIT_MESSAGES,
    threshold_hours: float = DEFAULT_REPLY_THRESHOLD_HOURS,
) -> ObserverDigestRun:
    """Build the morning Observer digest, optionally without sending it."""
    store = state_store or ObserverStateStore()
    active_client = client or await _get_userbot()
    if active_client is None:
        _append_manual_command(
            store,
            "digest_dry_run" if dry_run else "digest_run",
            actor_id=actor_id,
            status="skipped",
            metadata={"reason": "userbot_unavailable", "dry_run": dry_run},
        )
        return ObserverDigestRun(
            body="Observer userbot unavailable; digest was not generated.",
            snapshots=(),
            debts=(),
            dry_run=dry_run,
            skipped_reason="userbot_unavailable",
        )

    current_time = now or datetime.now(UTC)
    snapshots = tuple(
        await scanner(
            active_client,
            store,
            limit_dialogs=limit_dialogs,
            limit_messages_per_dialog=limit_messages_per_dialog,
            dry_run=dry_run,
        )
    )
    debts = tuple(detector(snapshots, current_time, threshold_hours=threshold_hours, state=store.load()))
    body = renderer(snapshots, debts, generated_at=current_time)
    sent = False
    if not dry_run:
        active_sender = sender or _default_sender()
        sent = bool(active_sender(body, parse_mode="HTML"))
        if sent:
            store.mark_digest("morning", _timestamp(current_time))

    _append_manual_command(
        store,
        "digest_dry_run" if dry_run else "digest_run",
        actor_id=actor_id,
        status="completed",
        metadata={
            "dry_run": dry_run,
            "sent": sent,
            "snapshot_count": len(snapshots),
            "debt_count": len(debts),
        },
    )
    return ObserverDigestRun(body=body, snapshots=snapshots, debts=debts, dry_run=dry_run, sent=sent)


async def run_reply_debts(
    *,
    client=None,
    state_store: ObserverStateStore | None = None,
    scanner=scan_private_dialogs,
    detector=detect_reply_debts,
    now: datetime | None = None,
    actor_id: str = "",
    limit_dialogs: int = DEFAULT_LIMIT_DIALOGS,
    limit_messages_per_dialog: int = DEFAULT_LIMIT_MESSAGES,
    threshold_hours: float = DEFAULT_REPLY_THRESHOLD_HOURS,
) -> ObserverDebtsRun:
    """Scan current private dialogs and return reply debts without sending messages."""
    store = state_store or ObserverStateStore()
    active_client = client or await _get_userbot()
    if active_client is None:
        _append_manual_command(
            store,
            "debts",
            actor_id=actor_id,
            status="skipped",
            metadata={"reason": "userbot_unavailable"},
        )
        return ObserverDebtsRun(debts=(), skipped_reason="userbot_unavailable")

    current_time = now or datetime.now(UTC)
    snapshots = await scanner(
        active_client,
        store,
        limit_dialogs=limit_dialogs,
        limit_messages_per_dialog=limit_messages_per_dialog,
        dry_run=True,
        unread_only=False,
    )
    debts = tuple(detector(tuple(snapshots), current_time, threshold_hours=threshold_hours, state=store.load()))
    _append_manual_command(
        store,
        "debts",
        actor_id=actor_id,
        metadata={"debt_count": len(debts), "snapshot_count": len(snapshots), "dry_run": True},
    )
    return ObserverDebtsRun(debts=debts)


def render_observer_debts(result: ObserverDebtsRun) -> str:
    """Render on-demand reply debts."""
    if result.skipped_reason:
        return "Observer userbot unavailable; reply debts were not scanned."
    if not result.debts:
        return "Reply debts: none."

    lines = ["Reply debts:"]
    for debt in result.debts:
        title = debt.peer_title or debt.peer_id
        excerpt = _plain_excerpt(debt.last_incoming_excerpt)
        lines.append(
            f"- {title}: {debt.severity}, {debt.hours_waiting:.1f}h waiting; "
            f"last incoming: {excerpt or 'no text excerpt'}"
        )
    return "\n".join(lines)


async def handle_observer_command(
    text: str,
    *,
    client=None,
    state_store: ObserverStateStore | None = None,
    scanner=scan_private_dialogs,
    detector=detect_reply_debts,
    digest_renderer=render_morning_observer_digest,
    is_dm: bool = True,
    actor_id: str = "",
) -> str | None:
    """Handle a `/observer ...` command or return None for non-Observer text."""
    stripped = (text or "").strip()
    if not stripped.casefold().startswith("/observer"):
        return None
    if not is_dm:
        return None

    store = state_store or ObserverStateStore()
    parts = stripped.split(maxsplit=3)
    command = parts[1].casefold() if len(parts) > 1 else "help"

    try:
        if command in {"help", "-h", "--help"}:
            return observer_help()
        if command == "status":
            _append_manual_command(store, "status", actor_id=actor_id)
            return render_observer_status(observer_status(state_store=store))
        if command == "ignore":
            peer = _required_arg(parts, "peer")
            reason = parts[3] if len(parts) > 3 else ""
            saved_peer = ignore_peer(peer, reason, state_store=store, actor_id=actor_id)
            return f"Observer: peer {saved_peer} added to ignore list."
        if command == "unignore":
            peer = _required_arg(parts, "peer")
            saved_peer = unignore_peer(peer, state_store=store, actor_id=actor_id)
            return f"Observer: peer {saved_peer} removed from ignore list."
        if command == "mute":
            peer = _required_arg(parts, "peer")
            reason = parts[3] if len(parts) > 3 else ""
            saved_peer = mute_peer(peer, reason, state_store=store, actor_id=actor_id)
            return f"Observer: peer {saved_peer} muted."
        if command == "unmute":
            peer = _required_arg(parts, "peer")
            saved_peer = unmute_peer(peer, state_store=store, actor_id=actor_id)
            return f"Observer: peer {saved_peer} unmuted."
        if command == "debts":
            return render_observer_debts(
                await run_reply_debts(
                    client=client,
                    state_store=store,
                    scanner=scanner,
                    detector=detector,
                    actor_id=actor_id,
                )
            )
        if command == "digest":
            mode = parts[2].casefold() if len(parts) > 2 else ""
            if mode not in {"dry-run", "--dry-run"}:
                return "Use `/observer digest dry-run` for safe diagnostics."
            result = await run_morning_digest(
                client=client,
                state_store=store,
                scanner=scanner,
                detector=detector,
                renderer=digest_renderer,
                dry_run=True,
                actor_id=actor_id,
            )
            return _format_digest_dry_run(result)
    except ValueError as exc:
        return f"Observer command error: {exc}\n\n{observer_help()}"

    return f"Unknown Observer command: {command}\n\n{observer_help()}"


def observer_help() -> str:
    """Return the supported v1 Observer commands."""
    return (
        "Observer commands:\n"
        "/observer status — show jobs, last runs, ignored/muted peers\n"
        "/observer ignore <peer> [reason] — skip peer in scans/debts\n"
        "/observer unignore <peer> — remove peer from ignore list\n"
        "/observer mute <peer> [reason] — silence peer in scans/debts\n"
        "/observer unmute <peer> — remove peer from mute list\n"
        "/observer debts — show current reply debts\n"
        "/observer digest dry-run — build morning digest without sending it"
    )


def _append_manual_command(
    store: ObserverStateStore,
    command: str,
    *,
    actor_id: str = "",
    peer_id: str = "",
    status: str = "completed",
    metadata: Mapping[str, Any] | None = None,
) -> None:
    safe_metadata: dict[str, Any] = {
        "command": command,
        "actor_id": mask_pii(str(actor_id or "")),
    }
    if peer_id:
        safe_metadata["peer_id"] = mask_pii(peer_id)
    if metadata:
        safe_metadata.update({str(key): _safe_metadata_value(value) for key, value in metadata.items()})

    store.append_run(
        ObserverRunResult(
            source_kind=MANUAL_SOURCE_KIND,
            run_id=f"manual:{command}:{utc_now_iso()}",
            status=status,
            metadata=safe_metadata,
        )
    )


def _safe_run_summary(run: Mapping[str, Any]) -> dict[str, Any]:
    def count(name: str) -> int:
        try:
            return int(run.get(name) or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "source_kind": _short(mask_pii(str(run.get("source_kind") or "unknown")), 80),
        "status": _short(mask_pii(str(run.get("status") or "unknown")), 40),
        "logged_at": _short(mask_pii(str(run.get("logged_at") or run.get("finished_at") or "")), 80),
        "scanned_count": count("scanned_count"),
        "captured_count": count("captured_count"),
        "skipped_count": count("skipped_count"),
        "error_count": count("error_count"),
    }


def _format_mapping(values: Mapping[str, str]) -> list[str]:
    if not values:
        return ["- none"]
    return [f"- {mask_pii(str(key))}: {mask_pii(str(value))}" for key, value in sorted(values.items())]


def _format_peer_list(peers: tuple[str, ...], reasons: Mapping[str, str]) -> list[str]:
    if not peers:
        return ["- none"]
    rows: list[str] = []
    for peer in peers:
        reason = reasons.get(peer, "")
        suffix = f" ({mask_pii(reason)})" if reason else ""
        rows.append(f"- {mask_pii(peer)}{suffix}")
    return rows


def _format_digest_dry_run(result: ObserverDigestRun) -> str:
    if result.skipped_reason:
        return result.body
    return (
        "Observer digest dry-run: nothing was sent.\n"
        f"Snapshots: {len(result.snapshots)}; debts: {len(result.debts)}\n\n"
        f"{_plain_digest(result.body)}"
    ).strip()


def _normalize_peer(peer_id: str) -> str:
    peer = str(peer_id or "").strip()
    if not peer:
        raise ValueError("peer is required")
    if any(ch.isspace() for ch in peer):
        raise ValueError("peer must be a single id/username token")
    return peer


def _required_arg(parts: list[str], name: str) -> str:
    if len(parts) < 3:
        raise ValueError(f"{name} is required")
    return parts[2]


def _clean_reason(reason: str) -> str:
    return mask_pii(_short(" ".join(str(reason or "").split()), 160))


def _safe_metadata_value(value: Any) -> Any:
    if isinstance(value, str):
        return mask_pii(_short(value, 200))
    if isinstance(value, bool | int | float) or value is None:
        return value
    return mask_pii(_short(str(value), 200))


def _plain_digest(body: str) -> str:
    without_tags = re.sub(r"</?[^>]+>", "", body or "")
    return mask_pii(without_tags)


def _plain_excerpt(text: str) -> str:
    return mask_pii(_short(" ".join((text or "").split()), 160))


def _short(text: str, limit: int) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _get_userbot():
    from kronos.telegram_client import get_userbot

    return await get_userbot()


def _default_sender() -> Callable[..., bool]:
    from kronos.cron.notify import send_bot_api

    return send_bot_api
