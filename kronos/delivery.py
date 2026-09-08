"""Transactional delivery queue with per-chunk receipts and kernel ownership."""

import asyncio
import json
import logging
import secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiosqlite

from kronos.db import SafeDB
from kronos.effect_state import DurableStateError
from kronos.security.output_validator import validate_output
from kronos.turn_ownership import TurnBusyError, own_conversation

log = logging.getLogger("kronos.delivery")
MAX_CHUNK_UNITS = 3500
SEND_TIMEOUT_SECONDS = 30


class DeliveryUncertainError(Exception):
    """The transport cannot safely establish or retry this delivery identity."""


class DeliveryRetryError(Exception):
    """A retryable transport error, optionally with a server-specified delay."""

    def __init__(self, delay: float = 0):
        super().__init__("delivery retry required")
        self.delay = delay


@dataclass(frozen=True)
class DeliveryChunk:
    """Frozen destination and message identity; never regenerate them on retry."""

    chat_id: int
    topic_id: int | None
    text: str
    random_id: int
    sender_id: int


def split_text(text: str) -> list[str]:
    """Split plain text at Unicode boundaries within Telegram UTF-16 limits."""
    chunks: list[str] = []
    start = units = 0
    for index, char in enumerate(text):
        if 0xD800 <= ord(char) <= 0xDFFF:
            raise ValueError("delivery text contains an unpaired surrogate")
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > MAX_CHUNK_UNITS:
            chunks.append(text[start:index])
            start, units = index, 0
        units += width
    if start < len(text):
        chunks.append(text[start:])
    return chunks


def enqueue(
    conn: sqlite3.Connection,
    *,
    event_key: str,
    stream_key: str,
    chat_id: int,
    topic_id: int | None,
    text: str,
    sender_id: int = 0,
    now: float | None = None,
) -> None:
    """Insert inside the producer's transaction; reject identity/content drift."""
    if not conn.in_transaction:
        raise DurableStateError("delivery enqueue requires the producer transaction")
    payload = _payload(event_key, stream_key, chat_id, topic_id, text, sender_id, now)
    conn.execute(_INSERT_DELIVERY, payload)
    existing = conn.execute(_SELECT_PAYLOAD, (event_key,)).fetchone()
    _check_payload(existing, payload)


_INSERT_DELIVERY = """INSERT INTO delivery_outbox
    (event_key, stream_key, chat_id, topic_id, chunks, random_ids, sender_id, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(event_key) DO NOTHING"""
_SELECT_PAYLOAD = "SELECT stream_key, chat_id, topic_id, chunks, sender_id FROM delivery_outbox WHERE event_key = ?"


def _check_payload(existing, payload) -> None:
    if not existing or tuple(existing[:4]) != tuple(payload[1:5]) or (payload[6] and existing[4] != payload[6]):
        raise DurableStateError("delivery identity reused with a different payload")


def _payload(event_key, stream_key, chat_id, topic_id, text, sender_id, now) -> tuple:
    if not event_key or not stream_key or type(chat_id) is not int or not chat_id:
        raise ValueError("delivery requires a key, stream and numeric destination")
    if topic_id is not None and (type(topic_id) is not int or topic_id <= 0):
        raise ValueError("delivery topic must be a positive message id")
    if type(sender_id) is not int or sender_id < 0:
        raise ValueError("delivery sender must be a non-negative account id")
    clean = validate_output(text).redacted_text.strip()
    if not clean:
        raise ValueError("delivery text must not be empty")
    chunks = json.dumps(split_text(clean), ensure_ascii=False)
    ids = json.dumps([secrets.randbits(63) or 1 for _ in json.loads(chunks)])
    stamp = time.time() if now is None else now
    return (event_key, stream_key, chat_id, topic_id, chunks, ids, sender_id, stamp, stamp)


async def enqueue_async(
    db: aiosqlite.Connection,
    *,
    event_key: str,
    stream_key: str,
    chat_id: int,
    topic_id: int | None,
    text: str,
    sender_id: int = 0,
    now: float | None = None,
) -> None:
    """Use the same frozen contract inside an async producer transaction."""
    if not db.in_transaction:
        raise DurableStateError("delivery enqueue requires the producer transaction")
    payload = _payload(event_key, stream_key, chat_id, topic_id, text, sender_id, now)
    await db.execute(_INSERT_DELIVERY, payload)
    async with db.execute(_SELECT_PAYLOAD, (event_key,)) as cursor:
        _check_payload(await cursor.fetchone(), payload)


def stream_status(db: SafeDB, stream_key: str) -> dict:
    """Return metadata only, not the notification text or recipient."""
    rows = db.read(
        "SELECT state, COUNT(*) AS count FROM delivery_outbox WHERE stream_key = ? AND obsolete = 0 GROUP BY state",
        (stream_key,),
    )
    counts = {row["state"]: row["count"] for row in rows}
    return {state: counts.get(state, 0) for state in ("pending", "delivered", "needs_review")}


async def drain(
    db: SafeDB,
    *,
    sender_id: int,
    send: Callable[[DeliveryChunk], Awaitable[int]],
    limit: int = 4,
    chunks_per_job: int = 2,
    now: Callable[[], float] = time.time,
) -> int:
    """Deliver bounded work; no lease expiry can steal a live sender's lock.

    A persisted Telegram random_id is reused after cancellation/crash. A receipt
    acknowledges server acceptance, not that the human read the message.
    """
    if not sender_id:
        return 0  # Not ready: no attempt, no binding to the wrong account.
    candidates = db.read(
        """SELECT * FROM delivery_outbox d WHERE state = 'pending' AND obsolete = 0 AND next_attempt <= ?
           AND NOT EXISTS (SELECT 1 FROM delivery_outbox earlier
               WHERE earlier.stream_key = d.stream_key AND earlier.seq < d.seq AND earlier.state != 'delivered' AND earlier.obsolete = 0)
           ORDER BY next_attempt, seq LIMIT ?""",
        (now(), limit),
    )
    completed = 0
    for candidate in candidates:
        key = candidate["event_key"]
        try:
            async with own_conversation(str(db.path), f"delivery:{key}", wait=False):
                completed += await _deliver_owned(db, key, candidate["seq"], sender_id, send, chunks_per_job, now)
        except TurnBusyError:
            continue
    return completed


async def _deliver_owned(
    db: SafeDB,
    key: str,
    sequence: int,
    sender_id: int,
    send: Callable[[DeliveryChunk], Awaitable[int]],
    chunks_per_job: int,
    now: Callable[[], float],
) -> int:
    """Keep failure/receipt commits inside the same ownership as dispatch."""
    try:
        row = dict(db.read_one("SELECT * FROM delivery_outbox WHERE event_key = ?", (key,)))
        if row["state"] != "pending" or row["obsolete"] or row["next_attempt"] > now():
            return 0
        if row["sender_id"] not in (0, sender_id):
            raise DeliveryUncertainError("sender account changed")
        try:
            texts, ids, receipts = json.loads(row["chunks"]), json.loads(row["random_ids"]), json.loads(row["receipts"])
            index = row["next_chunk"]
            if (
                not isinstance(texts, list)
                or not texts
                or not all(isinstance(s, str) and s for s in texts)
                or not isinstance(ids, list)
                or len(ids) != len(texts)
                or not all(type(value) is int and 0 < value < 2**63 for value in ids)
                or len(set(ids)) != len(ids)
                or not isinstance(receipts, list)
                or len(receipts) != index
                or not all(type(receipt) is int and receipt > 0 for receipt in receipts)
                or not 0 <= index < len(texts)
            ):
                raise ValueError("invalid payload")
        except (ValueError, TypeError) as error:
            raise DeliveryUncertainError("corrupt delivery state") from error
        db.write(
            "UPDATE delivery_outbox SET sender_id = ?, updated_at = ? WHERE event_key = ?",
            (sender_id, now(), key),
        )
        for _ in range(chunks_per_job):
            if db.read_one("SELECT obsolete FROM delivery_outbox WHERE event_key = ?", (key,))["obsolete"]:
                return 0
            db.write("UPDATE delivery_outbox SET attempts = attempts + 1 WHERE event_key = ?", (key,))
            chunk = DeliveryChunk(row["chat_id"], row["topic_id"], texts[index], ids[index], sender_id)
            receipt = await asyncio.wait_for(send(chunk), timeout=SEND_TIMEOUT_SECONDS)
            if type(receipt) is not int or receipt <= 0:
                raise DeliveryRetryError()
            receipts.append(receipt)
            index += 1
            state = "delivered" if index == len(texts) else "pending"
            db.write(
                """UPDATE delivery_outbox SET next_chunk = ?, receipts = ?, state = ?,
                   next_attempt = ?, last_error = '', updated_at = ? WHERE event_key = ?""",
                (index, json.dumps(receipts), state, now(), now(), key),
            )
            if state == "delivered":
                return 1
    except asyncio.CancelledError:
        raise  # The durable identity survives; no false acknowledgement.
    except Exception as error:
        # Error strings can contain full URLs, credentials or message text.
        # Keep only a bounded classifier; unknown transport results retry
        # the same random_id, never a new message identity.
        review = isinstance(error, DeliveryUncertainError)
        current = db.read_one("SELECT attempts, state FROM delivery_outbox WHERE event_key = ?", (key,))
        if current and current["state"] == "delivered":
            return 1  # The acknowledgement committed even if its caller saw an error.
        attempts = current["attempts"] if current else 0
        delay = max(min(3600, 5 * 2 ** min(attempts, 10)), getattr(error, "delay", 0))
        db.write(
            "UPDATE delivery_outbox SET state = ?, next_attempt = ?, last_error = ?, updated_at = ? WHERE event_key = ?",
            ("needs_review" if review else "pending", now() + delay, type(error).__name__, now(), key),
        )
        log.warning("Delivery #%s %s (%s)", sequence, "needs review" if review else "will retry", type(error).__name__)
    return 0
