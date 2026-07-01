"""Morning Observer digest for private Telegram dialogs."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime

from kronos.config import settings
from kronos.cron.notify import send_bot_api
from kronos.observer.daily_scope import build_daily_scope, local_day_window, save_daily_scope
from kronos.observer.render import render_daily_scope, render_morning_observer_digest
from kronos.observer.reply_debts import detect_reply_debts
from kronos.observer.state import ObserverStateStore
from kronos.observer.telegram_scan import scan_private_dialogs
from kronos.telegram_client import get_userbot

log = logging.getLogger("kronos.cron.personal_observer")

DEFAULT_LIMIT_DIALOGS = int(os.environ.get("OBSERVER_MAX_DIALOGS") or "50")
DEFAULT_LIMIT_MESSAGES = int(os.environ.get("OBSERVER_MAX_MESSAGES_PER_DIALOG") or "20")
DEFAULT_REPLY_THRESHOLD_HOURS = float(os.environ.get("OBSERVER_REPLY_THRESHOLD_HOURS") or "8")


async def run_personal_observer(
    *,
    client=None,
    state_store: ObserverStateStore | None = None,
    scanner=scan_private_dialogs,
    detector=detect_reply_debts,
    renderer=render_morning_observer_digest,
    sender: Callable[..., bool] = send_bot_api,
    now: datetime | None = None,
    limit_dialogs: int = DEFAULT_LIMIT_DIALOGS,
    limit_messages_per_dialog: int = DEFAULT_LIMIT_MESSAGES,
    threshold_hours: float = DEFAULT_REPLY_THRESHOLD_HOURS,
) -> bool:
    """Run the morning personal Observer digest."""
    if settings.agent_name != "kronos":
        log.info("Skipping personal observer on agent=%s", settings.agent_name)
        return False

    active_client = client or await get_userbot()
    if active_client is None:
        log.warning("Userbot unavailable; skipping personal observer digest")
        return False

    store = state_store or ObserverStateStore()
    current_time = now or datetime.now(UTC)
    snapshots = await scanner(
        active_client,
        store,
        limit_dialogs=limit_dialogs,
        limit_messages_per_dialog=limit_messages_per_dialog,
    )
    debts = detector(snapshots, current_time, threshold_hours=threshold_hours, state=store.load())
    digest = renderer(snapshots, debts, generated_at=current_time)
    sent = sender(digest, parse_mode="HTML")
    log.info(
        "Personal observer digest: unread=%d debts=%d sent=%s",
        len(snapshots),
        len(debts),
        sent,
    )
    return bool(sent)


async def run_daily_scope(
    *,
    client=None,
    state_store: ObserverStateStore | None = None,
    scanner=scan_private_dialogs,
    builder=build_daily_scope,
    renderer=render_daily_scope,
    saver=save_daily_scope,
    sender: Callable[..., bool] = send_bot_api,
    now: datetime | None = None,
    limit_dialogs: int = DEFAULT_LIMIT_DIALOGS,
    limit_messages_per_dialog: int = DEFAULT_LIMIT_MESSAGES,
) -> bool:
    """Run the evening Daily Scope digest."""
    if settings.agent_name != "kronos":
        return False
    active_client = client or await get_userbot()
    if active_client is None:
        log.warning("Userbot unavailable; skipping daily scope")
        return False

    store = state_store or ObserverStateStore()
    current_time = now or datetime.now(UTC)
    start, _end = local_day_window(current_time)
    snapshots = await scanner(
        active_client,
        store,
        limit_dialogs=limit_dialogs,
        limit_messages_per_dialog=limit_messages_per_dialog,
        since=start,
        unread_only=False,
    )
    entries = builder(snapshots, current_time)
    digest = renderer(entries, generated_at=current_time)
    saver(entries, workspace=store.workspace, day=current_time, body=digest)
    sent = sender(digest, parse_mode="HTML")
    log.info("Daily scope: entries=%d sent=%s", len(entries), sent)
    return bool(sent)
