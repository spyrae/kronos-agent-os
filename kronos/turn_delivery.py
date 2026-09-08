"""Recovery notification producers; routing comes from transport, never text."""

import json
from dataclasses import asdict, dataclass

import aiosqlite

from kronos.delivery import enqueue_async
from kronos.effect_state import DurableStateError


@dataclass(frozen=True)
class RecoveryDestination:
    """Trusted Telegram provenance recorded before the original model call."""

    chat_id: int
    sender_id: int
    topic_id: int | None = None
    review_required: bool = False

    def __post_init__(self):
        if type(self.chat_id) is not int or not self.chat_id or type(self.sender_id) is not int or self.sender_id <= 0:
            raise ValueError("recovery requires a numeric destination and authenticated sender")
        if self.topic_id is not None and (type(self.topic_id) is not int or self.topic_id <= 0):
            raise ValueError("invalid recovery topic")
        if type(self.review_required) is not bool:
            raise ValueError("invalid recovery review policy")

    def encode(self) -> str:
        """Persist only transport provenance, without usernames or credentials."""
        return json.dumps(asdict(self), sort_keys=True)

    def assert_thread(self, thread_id: str) -> None:
        """A route cannot be attached to a different conversation."""
        expected = str(self.chat_id) + (f":{self.topic_id}" if self.topic_id else "")
        if thread_id != expected:
            raise DurableStateError("recovery destination does not match the conversation")

    @classmethod
    def decode(cls, raw: str) -> "RecoveryDestination":
        """Reject malformed or unknown metadata rather than guessing a route."""
        try:
            return cls(**json.loads(raw))
        except (TypeError, ValueError) as error:
            raise DurableStateError("invalid recovery destination") from error


async def _destination(db: aiosqlite.Connection, turn_id: str) -> RecoveryDestination | None:
    cursor = await db.execute(
        "SELECT recovery_destination, delivery_requested, thread_id FROM active_turns WHERE turn_id = ?", (turn_id,)
    )
    row = await cursor.fetchone()
    if not row or not row[1]:
        return None
    issue = "destination_missing"
    if row[0]:
        try:
            destination = RecoveryDestination.decode(row[0])
            destination.assert_thread(row[2])
            return destination
        except DurableStateError:
            issue = "destination_invalid"
    await db.execute("UPDATE active_turns SET delivery_issue = ? WHERE turn_id = ?", (issue, turn_id))
    return None


async def queue_result(db: aiosqlite.Connection, turn_id: str, content: str | None, *, failed: bool = False) -> None:
    """Queue the exact terminal response in its finalization transaction."""
    if not db.in_transaction:
        raise DurableStateError("turn delivery requires its producer transaction")
    await db.execute(
        "UPDATE delivery_outbox SET obsolete = 1 WHERE stream_key = ? AND event_key != ?",
        (f"turn:{turn_id}", f"turn:{turn_id}:result"),
    )
    destination = await _destination(db, turn_id)
    if destination is None:
        return
    text = (content or "").strip()
    if failed:
        text = "⚠️ Восстановление не завершено. Проверь состояние turn и возможные внешние эффекты." + (
            f"\n\n{text}" if text else ""
        )
    if not text:
        await db.execute("UPDATE active_turns SET delivery_issue = 'result_missing' WHERE turn_id = ?", (turn_id,))
        return
    text = f"Восстановление · turn {turn_id}\n\n{text}"
    key = f"turn:{turn_id}:result"
    await enqueue_async(
        db,
        event_key=key,
        stream_key=f"turn:{turn_id}",
        chat_id=destination.chat_id,
        topic_id=destination.topic_id,
        sender_id=destination.sender_id,
        text=text,
    )
    if destination.review_required:
        await db.execute(
            "UPDATE delivery_outbox SET state = 'needs_review', last_error = 'outbound_review_required' WHERE event_key = ?",
            (key,),
        )
    await db.execute("UPDATE active_turns SET delivery_issue = '' WHERE turn_id = ?", (turn_id,))


async def queue_approval(db: aiosqlite.Connection, turn_id: str, approval_id: str) -> None:
    """An approval wait is a durable notice, never a completed answer."""
    if not db.in_transaction:
        raise DurableStateError("approval delivery requires its producer transaction")
    destination = await _destination(db, turn_id)
    if destination is None:
        return
    cursor = await db.execute(
        "SELECT tool_name, args_json FROM pending_approvals WHERE approval_id = ? AND turn_id = ? AND status = 'pending'",
        (approval_id, turn_id),
    )
    row = await cursor.fetchone()
    if row is None:
        return
    arguments = row[1]
    complete = len(arguments) <= 2500
    if not complete:
        arguments = "Аргументы слишком длинные: проверь approval в CLI перед подтверждением."
    text = f"Восстановление ожидает подтверждения.\nTool: {row[0]}\nАргументы: {arguments}\n\n"
    if complete:
        text += f"Подтвердить: /approve {approval_id}\n"
    text += f"Отклонить: /reject {approval_id}"
    await enqueue_async(
        db,
        event_key=f"turn:{turn_id}:approval:{approval_id}",
        stream_key=f"turn:{turn_id}",
        chat_id=destination.chat_id,
        topic_id=destination.topic_id,
        sender_id=destination.sender_id,
        text=text,
    )


async def suppress_approval(db: aiosqlite.Connection, turn_id: str, approval_id: str) -> None:
    """Suppress obsolete work without inventing a send acknowledgement.

    An already dispatched chunk may still arrive. Its commands recheck the
    durable decision; neither late delivery nor suppression authorizes a tool.
    """
    await db.execute(
        "UPDATE delivery_outbox SET obsolete = 1 WHERE event_key = ?",
        (f"turn:{turn_id}:approval:{approval_id}",),
    )
