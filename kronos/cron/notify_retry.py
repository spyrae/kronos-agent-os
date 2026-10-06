"""Retry cron notifications that Telegram verifiably did not accept.

Only a definite non-acceptance is queued: Telegram answered 429 or 5xx, or the
connection could not be opened at all. A request that was sent and then timed
out has an unknown outcome and is not repeated automatically.

The queue stores the message body, never the Bot API URL: the URL carries the
bot token.
"""

import json
import logging
import time

from kronos.db import get_db

log = logging.getLogger("kronos.cron.notify_retry")

MAX_ATTEMPTS = 6
MAX_AGE_SECONDS = 24 * 3600
_SCHEMA = """
    CREATE TABLE IF NOT EXISTS notify_retry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        body TEXT NOT NULL,
        reason TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL,
        created_at REAL NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'dead'))
    );
    CREATE INDEX IF NOT EXISTS idx_notify_retry_due ON notify_retry(state, next_attempt);
"""


def _db():
    db = get_db("notify_retry")
    db.init_schema(lambda conn: conn.executescript(_SCHEMA))
    return db


def _backoff(attempts: int) -> float:
    return min(3600.0, 60.0 * 2**attempts)


def queue_retry(body: dict, reason: str, *, now: float | None = None) -> None:
    """Remember a notification Telegram did not accept. Never raises into the sender."""
    current = time.time() if now is None else now
    try:
        _db().write(
            "INSERT INTO notify_retry (body, reason, next_attempt, created_at) VALUES (?, ?, ?, ?)",
            (json.dumps(body, ensure_ascii=False), reason[:80], current + _backoff(0), current),
        )
        log.warning("Notification queued for retry: %s", reason[:80])
    except Exception as error:
        log.error("Notification could not be queued for retry: %s", type(error).__name__)


def pending_count() -> dict[str, int]:
    """Queue size by state, for health and tests."""
    rows = _db().read("SELECT state, COUNT(*) AS cnt FROM notify_retry GROUP BY state")
    counts = {row["state"]: row["cnt"] for row in rows}
    return {"pending": counts.get("pending", 0), "dead": counts.get("dead", 0)}


def retry_due(*, limit: int = 10, now: float | None = None) -> int:
    """Send notifications whose retry time has come. Returns how many were delivered."""
    from kronos.cron import notify

    current = time.time() if now is None else now
    db = _db()
    rows = db.read(
        "SELECT * FROM notify_retry WHERE state = 'pending' AND next_attempt <= ? ORDER BY id LIMIT ?",
        (current, limit),
    )
    delivered = 0
    for row in rows:
        try:
            body = json.loads(row["body"])
        except (ValueError, TypeError):
            db.write("UPDATE notify_retry SET state = 'dead', reason = 'corrupt payload' WHERE id = ?", (row["id"],))
            continue
        outcome = notify.send_prepared(body)
        attempts = row["attempts"] + 1
        if outcome == "sent":
            db.write("DELETE FROM notify_retry WHERE id = ?", (row["id"],))
            delivered += 1
        elif outcome == "retry" and attempts < MAX_ATTEMPTS and current - row["created_at"] < MAX_AGE_SECONDS:
            db.write(
                "UPDATE notify_retry SET attempts = ?, next_attempt = ? WHERE id = ?",
                (attempts, current + _backoff(attempts), row["id"]),
            )
        else:
            # Refused for good, outcome unknown, or the retry budget is spent.
            reason = "retry budget exhausted" if outcome == "retry" else f"gave up: {outcome}"
            db.write(
                "UPDATE notify_retry SET state = 'dead', attempts = ?, reason = ? WHERE id = ?",
                (attempts, reason, row["id"]),
            )
            if outcome == "retry":
                destination = notify.describe_destination(body.get("chat_id"), body.get("message_thread_id"))
                notify.report_undelivered(destination, reason, str(body.get("text", "")))
    return delivered


async def run_notify_retry() -> None:
    """Cron entry point."""
    import asyncio

    delivered = await asyncio.to_thread(retry_due)
    if delivered:
        log.info("Delivered %d queued notification(s)", delivered)
