"""Session store — persistent conversation history per thread_id.

Replaces LangGraph's AsyncSqliteSaver checkpointer.
Stores messages as JSON in SQLite, keyed by thread_id.
"""

import asyncio
import hashlib
import json
import logging
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from kronos.effect_state import DurableStateError, EffectClaim, EffectUncertainError
from kronos.migrations.v001_turn_outcome import migrate as migrate_turn_outcome
from kronos.migrations.v003_effect_intents import migrate as migrate_effect_intents
from kronos.migrations.v004_effect_protocol import migrate as migrate_effect_protocol
from kronos.migrations.v005_plan_execution import migrate_turns as migrate_caller_key
from kronos.migrations.v008_turn_delivery import migrate as migrate_delivery
from kronos.outcomes import InvocationOutcome, InvocationStatus
from kronos.tool_history import trim_completed_history, unanswered_tool_calls
from kronos.turn_delivery import RecoveryDestination, queue_approval, queue_result, suppress_approval
from kronos.turn_ownership import TurnBusyError, TurnOwnership, own_conversation

log = logging.getLogger("kronos.session")

# Max messages to keep in history (oldest are dropped on save).
# Keep small — large history causes LLM to copy prior patterns
# (including hallucinated tool calls) instead of using tools.
MAX_HISTORY = 30

# A pending tool approval older than this is treated as stale: claiming it
# returns nothing and marks it expired. Stops a long-forgotten "restart the
# server?" prompt from firing hours later, in a context that no longer holds.
APPROVAL_TTL_SECONDS = 3600


def _approval_is_stale(requested_at: object) -> bool:
    """True if a pending approval's requested_at is older than the TTL."""
    if not requested_at:
        return False
    try:
        requested_dt = datetime.fromisoformat(str(requested_at))
    except (ValueError, TypeError):
        return False
    if requested_dt.tzinfo is None:
        requested_dt = requested_dt.replace(tzinfo=UTC)
    return (datetime.now(UTC) - requested_dt).total_seconds() > APPROVAL_TTL_SECONDS


def _session_fts_fingerprint(
    *,
    agent_name: str,
    thread_id: str,
    position: int,
    role: str,
    content: str,
) -> str:
    """Stable key for idempotent cross-session FTS indexing."""
    payload = json.dumps(
        [agent_name, thread_id, position, role, content],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _serialize_message(msg: BaseMessage) -> dict:
    """Serialize a LangChain message to a JSON-safe dict."""
    data = {
        "type": msg.__class__.__name__,
        "content": msg.content,
    }
    if hasattr(msg, "tool_calls") and msg.tool_calls:
        data["tool_calls"] = msg.tool_calls
    if hasattr(msg, "tool_call_id") and msg.tool_call_id:
        data["tool_call_id"] = msg.tool_call_id
    return data


def _safe_json(raw: str) -> dict:
    """Parse a journal payload, tolerating a corrupted row."""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _deserialize_message(data: dict) -> BaseMessage:
    """Deserialize a dict back to a LangChain message."""
    msg_type = data.get("type", "HumanMessage")
    content = data.get("content", "")

    if msg_type == "HumanMessage":
        return HumanMessage(content=content)
    elif msg_type == "AIMessage":
        msg = AIMessage(content=content)
        if data.get("tool_calls"):
            msg.tool_calls = data["tool_calls"]
        return msg
    elif msg_type == "SystemMessage":
        return SystemMessage(content=content)
    elif msg_type == "ToolMessage":
        return ToolMessage(
            content=content,
            tool_call_id=data.get("tool_call_id", ""),
        )
    else:
        return HumanMessage(content=content)


def _deserialize_journal_message(raw: str) -> BaseMessage:
    """Decode execution evidence strictly; never silently skip a corrupt delta."""
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("type") not in {
            "AIMessage",
            "HumanMessage",
            "SystemMessage",
            "ToolMessage",
        }:
            raise ValueError("invalid journal message type")
        if not isinstance(data.get("content"), (str, list)):
            raise ValueError("invalid journal content")
        if data["type"] == "AIMessage" and not isinstance(data.get("tool_calls", []), list):
            raise ValueError("invalid journal tool calls")
        if data["type"] == "ToolMessage" and not (isinstance(data.get("tool_call_id"), str) and data["tool_call_id"]):
            raise ValueError("invalid journal tool result id")
        return _deserialize_message(data)
    except (ValueError, KeyError, TypeError) as error:
        raise DurableStateError("invalid durable journal message; review required") from error


class SessionStore:
    """Async SQLite-based session store for conversation history."""

    def __init__(self, db_path: str, agent_name: str = ""):
        self.db_path = db_path
        self._agent_name = agent_name
        self._initialized = False

    @asynccontextmanager
    async def _open_db(self):
        """Open a connection with WAL mode and generous busy timeout."""
        async with aiosqlite.connect(self.db_path, timeout=30) as db:
            await db.execute("PRAGMA busy_timeout=30000")
            deadline = asyncio.get_running_loop().time() + 30
            while True:
                try:
                    async with db.execute("PRAGMA journal_mode") as cursor:
                        mode = await cursor.fetchone()
                    if mode[0].lower() != "wal":
                        await db.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as error:
                    # Concurrent first connections can both try the WAL mode
                    # transition, which may fail immediately despite timeout.
                    code = getattr(error, "sqlite_errorcode", 0) & 0xFF
                    if code != sqlite3.SQLITE_BUSY or asyncio.get_running_loop().time() >= deadline:
                        raise
                    await asyncio.sleep(0.05)
            await db.execute("PRAGMA wal_autocheckpoint=100")
            yield db

    async def _ensure_table(self, db: aiosqlite.Connection) -> None:
        """Create sessions table if it doesn't exist."""
        if not self._initialized:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    thread_id TEXT PRIMARY KEY,
                    messages TEXT NOT NULL DEFAULT '[]',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS active_turns (
                    turn_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_message TEXT NOT NULL,
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    error TEXT
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_active_turns_running
                    ON active_turns(status, started_at)
            """)
            # attempts arrived with durable resume: a turn that keeps dying must
            # not be retried forever. Backfill on databases created before it.
            cursor = await db.execute("PRAGMA table_info(active_turns)")
            turn_columns = {row[1] for row in await cursor.fetchall()}
            if "attempts" not in turn_columns:
                try:
                    await db.execute("ALTER TABLE active_turns ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
                except sqlite3.OperationalError as e:
                    if "duplicate column name" not in str(e).lower():
                        raise
            await db.execute("""
                CREATE TABLE IF NOT EXISTS turn_journal (
                    turn_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    message_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'appended',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (turn_id, seq)
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_turn_journal_thread
                    ON turn_journal(thread_id, created_at)
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tool_results (
                    turn_id TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (turn_id, tool_call_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS pending_approvals (
                    approval_id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    args_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    decided_at TIMESTAMP,
                    decided_by TEXT,
                    decision TEXT,
                    delegation_json TEXT
                )
            """)
            # delegation_json arrived with nested sub-agent approvals (it records
            # which delegate_to_X call to re-run on resume). Backfill it on
            # databases created before the column existed.
            cursor = await db.execute("PRAGMA table_info(pending_approvals)")
            columns = {row[1] for row in await cursor.fetchall()}
            if "delegation_json" not in columns:
                try:
                    await db.execute("ALTER TABLE pending_approvals ADD COLUMN delegation_json TEXT")
                except sqlite3.OperationalError as e:
                    # The PRAGMA check and ALTER aren't atomic across connections,
                    # so a concurrent SessionStore instance may add the column
                    # first at startup. A duplicate-column error means it now
                    # exists — which is exactly the desired end state.
                    if "duplicate column name" not in str(e).lower():
                        raise
            await db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_approvals_turn_call
                    ON pending_approvals(turn_id, tool_call_id)
                    WHERE status = 'pending'
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_pending_approvals_status
                    ON pending_approvals(status, requested_at)
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS external_effects (
                    idempotency_key TEXT PRIMARY KEY,
                    turn_id         TEXT NOT NULL,
                    tool            TEXT NOT NULL,
                    result          TEXT NOT NULL,
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_external_effects_turn
                    ON external_effects(turn_id, created_at)
            """)
            await db.commit()
            await migrate_turn_outcome(db)
            await migrate_effect_intents(db)
            await migrate_effect_protocol(db)
            await migrate_caller_key(db)
            await migrate_delivery(db)
            self._initialized = True

    async def begin_turn(
        self,
        thread_id: str,
        input_message: str,
        *,
        caller_key: str = "",
        recovery_destination: RecoveryDestination | None = None,
    ) -> str:
        """Open a turn, recording the caller's immutable correlation atomically."""
        if recovery_destination is not None:
            recovery_destination.assert_thread(thread_id)
        turn_id = str(uuid.uuid4())
        async with self._open_db() as db:
            await self._ensure_table(db)
            await db.execute(
                """
                INSERT INTO active_turns
                    (turn_id, thread_id, status, input_message, effect_protocol, caller_key, recovery_destination)
                VALUES (?, ?, 'running', ?, 1, ?, ?)
                """,
                (
                    turn_id,
                    thread_id,
                    input_message,
                    caller_key,
                    recovery_destination.encode() if recovery_destination else "",
                ),
            )
            await db.commit()
        return turn_id

    async def get_turn_for_caller(self, caller_key: str) -> dict | None:
        """Read identity only, so corrupt journal content cannot hide a claim."""
        if not caller_key:
            return None
        async with self._open_db() as db:
            await self._ensure_table(db)
            cursor = await db.execute(
                "SELECT turn_id, thread_id, status, caller_key FROM active_turns WHERE caller_key = ?",
                (caller_key,),
            )
            row = await cursor.fetchone()
        return dict(zip(("turn_id", "thread_id", "status", "caller_key"), row, strict=True)) if row else None

    async def append_turn_messages(
        self,
        *,
        turn_id: str,
        thread_id: str,
        messages: list[BaseMessage],
    ) -> None:
        """Append message deltas to a durable turn journal."""
        if not messages:
            return

        async with self._open_db() as db:
            await self._ensure_table(db)
            cursor = await db.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM turn_journal WHERE turn_id = ?",
                (turn_id,),
            )
            row = await cursor.fetchone()
            next_seq = int(row[0]) + 1 if row else 1
            await db.executemany(
                """
                INSERT INTO turn_journal
                    (turn_id, thread_id, seq, message_json)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        turn_id,
                        thread_id,
                        next_seq + offset,
                        json.dumps(_serialize_message(message), ensure_ascii=False),
                    )
                    for offset, message in enumerate(messages)
                ],
            )
            await db.commit()

    async def get_tool_result(self, turn_id: str, tool_call_id: str) -> str | None:
        """Return memoized tool content for this turn/tool call, if present."""
        async with self._open_db() as db:
            await self._ensure_table(db)
            cursor = await db.execute(
                """
                SELECT content FROM tool_results
                WHERE turn_id = ? AND tool_call_id = ?
                """,
                (turn_id, tool_call_id),
            )
            row = await cursor.fetchone()
        return str(row[0]) if row else None

    async def get_recorded_call_effect(self, turn_id: str, tool_call: dict) -> str | None:
        """Read a proven effect result using the frozen call identity, not a new key."""
        args_json = json.dumps(tool_call["args"], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        async with self._open_db() as db:
            await self._ensure_table(db)
            cursor = await db.execute(
                """SELECT e.result FROM effect_intents i
                   JOIN external_effects e ON e.idempotency_key = i.idempotency_key
                   WHERE i.turn_id = ? AND i.tool_call_id = ? AND i.tool = ?
                     AND i.args_json = ? AND i.status = 'recorded'""",
                (turn_id, tool_call["id"], tool_call["name"], args_json),
            )
            rows = await cursor.fetchall()
        if len(rows) > 1:
            raise EffectUncertainError("multiple effects for one tool call; reconcile before continuing")
        return str(rows[0][0]) if rows else None

    async def save_tool_result(
        self,
        *,
        turn_id: str,
        tool_call_id: str,
        content: str,
    ) -> None:
        """Memoize a tool result for the active durable turn."""
        if not tool_call_id:
            return
        async with self._open_db() as db:
            await self._ensure_table(db)
            await db.execute(
                """
                INSERT OR IGNORE INTO tool_results
                    (turn_id, tool_call_id, content)
                VALUES (?, ?, ?)
                """,
                (turn_id, tool_call_id, content),
            )
            await db.commit()

    async def list_turns(self, *, status: str = "", thread_id: str = "", limit: int = 20) -> list[dict]:
        """Recent durable turns, newest first."""
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if thread_id:
            clauses.append("thread_id = ?")
            params.append(thread_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        async with self._open_db() as db:
            await self._ensure_table(db)
            cursor = await db.execute(
                f"""
                SELECT turn_id, thread_id, status, input_message, attempts, started_at, completed_at, error
                FROM active_turns {where} ORDER BY rowid DESC LIMIT ?
                """,
                tuple(params),
            )
            rows = await cursor.fetchall()

        keys = ("turn_id", "thread_id", "status", "input_message", "attempts", "started_at", "completed_at", "error")
        return [dict(zip(keys, row, strict=False)) for row in rows]

    async def recovery_destination(self, turn_id: str) -> RecoveryDestination | None:
        """Read verified provenance; missing legacy routes never become guesses."""
        async with self._open_db() as db:
            await self._ensure_table(db)
            async with db.execute(
                "SELECT thread_id, recovery_destination FROM active_turns WHERE turn_id = ?", (turn_id,)
            ) as cursor:
                row = await cursor.fetchone()
        if not row or not row[1]:
            return None
        destination = RecoveryDestination.decode(row[1])
        destination.assert_thread(row[0])
        return destination

    async def delivery_status(self, turn_id: str) -> dict:
        """Separate execution from transport acceptance, without exposing payloads."""
        async with self._open_db() as db:
            await self._ensure_table(db)
            await db.execute("BEGIN")
            async with db.execute(
                "SELECT delivery_requested, delivery_issue FROM active_turns WHERE turn_id = ?", (turn_id,)
            ) as cursor:
                turn = await cursor.fetchone()
            async with db.execute(
                "SELECT event_key, state, obsolete FROM delivery_outbox WHERE stream_key = ?",
                (f"turn:{turn_id}",),
            ) as cursor:
                rows = await cursor.fetchall()
        counts = {key: 0 for key in ("pending", "delivered", "needs_review", "obsolete")}
        state = "awaiting_result" if turn and turn[0] else "not_requested"
        for key, delivery, obsolete in rows:
            counts["obsolete" if obsolete else delivery] += 1
            if key == f"turn:{turn_id}:result" and not obsolete:
                state = delivery
        return {"requested": bool(turn and turn[0]), "state": (turn[1] if turn else "turn_missing") or state, **counts}

    async def deliver_pending(self) -> int:
        """Drain this producer DB independently of model execution and webhooks."""
        from kronos.db import SafeDB
        from kronos.delivery import drain
        from kronos.telegram_delivery import ready_sender, send_chunk

        await self.expire_recovery_approvals()
        async with self._open_db() as db:
            await self._ensure_table(db)
        queue = SafeDB(Path(self.db_path))
        try:
            return await drain(queue, sender_id=ready_sender(), send=send_chunk)
        finally:
            queue.close()

    async def expire_recovery_approvals(self, limit: int = 4) -> int:
        """Reconcile expired recovery gates only after excluding a live owner."""
        async with self._open_db() as db:
            await self._ensure_table(db)
            async with db.execute(
                """SELECT p.approval_id, p.turn_id, p.thread_id, p.requested_at
                   FROM pending_approvals p JOIN active_turns t ON t.turn_id = p.turn_id
                   WHERE p.status = 'pending' AND t.delivery_requested = 1
                   ORDER BY p.requested_at LIMIT ?""",
                (limit,),
            ) as cursor:
                candidates = await cursor.fetchall()
        expired = 0
        for approval_id, turn_id, thread_id, requested in candidates:
            if not _approval_is_stale(requested):
                continue
            try:
                async with own_conversation(self.db_path, thread_id, wait=False):
                    async with self._open_db() as db:
                        await db.execute("BEGIN IMMEDIATE")
                        async with db.execute(
                            "SELECT requested_at FROM pending_approvals WHERE approval_id = ? AND status = 'pending'",
                            (approval_id,),
                        ) as cursor:
                            row = await cursor.fetchone()
                        if row and _approval_is_stale(row[0]):
                            await self._expire_pending_approval(db, turn_id, approval_id)
                            expired += 1
                        await db.commit()
            except TurnBusyError:
                continue
        return expired

    @staticmethod
    async def _expire_pending_approval(db, turn_id: str, approval_id: str) -> None:
        await db.execute(
            "UPDATE pending_approvals SET status = 'expired' WHERE approval_id = ? AND status = 'pending'",
            (approval_id,),
        )
        await suppress_approval(db, turn_id, approval_id)
        cursor = await db.execute(
            """UPDATE active_turns SET status = 'failed', error = 'approval_expired', completed_at = CURRENT_TIMESTAMP
               WHERE turn_id = ? AND status IN ('running', 'resuming', 'waiting_approval')""",
            (turn_id,),
        )
        if cursor.rowcount:
            await queue_result(db, turn_id, None, failed=True)

    async def get_turn_detail(self, turn_id: str) -> dict | None:
        """One turn with its journal, memoized tool results and effects."""
        async with self._open_db() as db:
            await self._ensure_table(db)
            cursor = await db.execute(
                """
                SELECT turn_id, thread_id, status, input_message, attempts, started_at, completed_at, error, effect_protocol, caller_key,
                       final_content, delivery_requested, delivery_issue
                FROM active_turns WHERE turn_id = ?
                """,
                (turn_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            keys = (
                "turn_id",
                "thread_id",
                "status",
                "input_message",
                "attempts",
                "started_at",
                "completed_at",
                "error",
                "effect_protocol",
                "caller_key",
                "final_content",
                "delivery_requested",
                "delivery_issue",
            )
            turn = dict(zip(keys, row, strict=False))

            journal_cursor = await db.execute(
                "SELECT seq, message_json, status, created_at FROM turn_journal WHERE turn_id = ? ORDER BY seq",
                (turn_id,),
            )
            turn["journal"] = [
                {"seq": entry[0], "message": _safe_json(entry[1]), "status": entry[2], "created_at": str(entry[3])}
                for entry in await journal_cursor.fetchall()
            ]

            results_cursor = await db.execute(
                "SELECT tool_call_id, content FROM tool_results WHERE turn_id = ?",
                (turn_id,),
            )
            turn["tool_results"] = [
                {"tool_call_id": entry[0], "content": entry[1]} for entry in await results_cursor.fetchall()
            ]

        turn["effects"] = await self.list_external_effects(turn_id)
        return turn

    async def fork_turn(self, turn_id: str, *, at_seq: int = 0, new_thread_id: str = "") -> dict | None:
        """Copy a turn's history prefix into a new thread.

        The original is left untouched — the point of a fork is to try a
        different continuation without destroying the evidence of the first one.
        """
        detail = await self.get_turn_detail(turn_id)
        if not detail:
            return None

        prefix = detail["journal"] if at_seq <= 0 else [row for row in detail["journal"] if int(row["seq"]) <= at_seq]
        messages: list[BaseMessage] = [HumanMessage(content=str(detail.get("input_message") or ""))]
        for row in prefix:
            try:
                messages.append(_deserialize_message(row["message"]))
            except (KeyError, TypeError):
                continue

        target = new_thread_id or f"{detail['thread_id']}:fork-{at_seq or len(prefix)}"
        await self.save(target, messages)
        log.info("Forked turn %s into thread %s (%d message(s))", turn_id, target, len(messages))
        return {"thread_id": target, "messages": len(messages), "source_turn": turn_id}

    async def running_turn_stats(self) -> dict:
        """Counts and oldest age for turns in flight — surfaced on /health.

        A turn stuck in 'running' means a process died and nothing picked it up;
        without this it is invisible until someone reads the database.
        """
        async with self._open_db() as db:
            await self._ensure_table(db)
            cursor = await db.execute(
                """
                SELECT COUNT(*), MIN(started_at)
                FROM active_turns WHERE status IN ('running', 'resuming')
                """
            )
            count, oldest = await cursor.fetchone()

        age_seconds = None
        if oldest:
            from datetime import UTC, datetime

            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
                try:
                    # SQLite CURRENT_TIMESTAMP is UTC but naive, so attach UTC
                    # rather than comparing against a naive local clock.
                    started = datetime.strptime(str(oldest), fmt).replace(tzinfo=UTC)
                    age_seconds = round((datetime.now(UTC) - started).total_seconds(), 1)
                    break
                except ValueError:
                    continue
        return {"running_turns": int(count or 0), "oldest_running_age_seconds": age_seconds}

    async def prune_turn_history(self, *, older_than_days: int = 30) -> dict:
        """Delete finished turns and whatever still hangs off them.

        Caller-owned identities are retained: deleting one could turn a lost
        reverse link into false proof that its external work never started.
        Only finished turns: a running or resuming turn is live state, and an
        unfinished turn older than the window is a bug to look at, not garbage to
        sweep.

        In practice the journal and memoized results are already gone —
        ``finish_turn`` drops them when a turn completes — so the rows this
        actually reclaims are ``active_turns`` and ``external_effects``. Effects
        are safe to drop with their turn: the idempotency key contains the
        turn_id, so a later turn never looks up an older turn's effect.
        """
        async with self._open_db() as db:
            await self._ensure_table(db)
            cutoff = f"-{int(older_than_days)} days"
            cursor = await db.execute(
                """
                SELECT turn_id FROM active_turns
                WHERE status NOT IN ('running', 'resuming', 'waiting_approval')
                  AND caller_key = '' AND delivery_requested = 0
                  AND COALESCE(completed_at, started_at) < datetime('now', ?)
                  AND NOT EXISTS (SELECT 1 FROM effect_intents i
                      WHERE i.turn_id = active_turns.turn_id AND i.status = 'pending')
                """,
                (cutoff,),
            )
            turn_ids = [row[0] for row in await cursor.fetchall()]
            if not turn_ids:
                return {"turns": 0, "journal": 0, "tool_results": 0, "effects": 0}

            placeholders = ",".join("?" for _ in turn_ids)
            journal = await db.execute(f"DELETE FROM turn_journal WHERE turn_id IN ({placeholders})", turn_ids)
            results = await db.execute(f"DELETE FROM tool_results WHERE turn_id IN ({placeholders})", turn_ids)
            effects = await db.execute(f"DELETE FROM external_effects WHERE turn_id IN ({placeholders})", turn_ids)
            await db.execute(f"DELETE FROM effect_intents WHERE turn_id IN ({placeholders})", turn_ids)
            turns = await db.execute(f"DELETE FROM active_turns WHERE turn_id IN ({placeholders})", turn_ids)
            await db.commit()

        pruned = {
            "turns": turns.rowcount,
            "journal": journal.rowcount,
            "tool_results": results.rowcount,
            "effects": effects.rowcount,
        }
        log.info("Turn retention: pruned %s", pruned)
        return pruned

    async def resumable_turns(self) -> list[dict]:
        """List unowned candidates; plan recovery must reconcile its claim first."""
        async with self._open_db() as db:
            await self._ensure_table(db)
            cursor = await db.execute(
                """SELECT turn_id, thread_id FROM active_turns
                   WHERE status IN ('running', 'resuming') AND caller_key = ''
                     AND thread_id NOT LIKE 'plan:%' ORDER BY rowid ASC"""
            )
            return [{"turn_id": row[0], "thread_id": row[1]} for row in await cursor.fetchall()]

    async def claim_turn_for_resume(
        self, turn_id: str, *, ownership: TurnOwnership, max_attempts: int = 2, notify: bool = False
    ) -> dict | None:
        """Claim one abandoned turn while its conversation stays exclusively held.

        Both running and resuming rows can be left by a crash. The kernel lock,
        not a timestamp or status flag, proves no cooperating executor is live.
        Caller must keep ownership through the entire continuation.
        """
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        claimed = None
        async with self._open_db() as db:
            await self._ensure_table(db)
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """SELECT thread_id, input_message, attempts, rowid
                       FROM active_turns WHERE turn_id = ? AND status IN ('running', 'resuming')""",
                    (turn_id,),
                )
                row = await cursor.fetchone()
                if not row:
                    await db.rollback()
                    return None
                thread_id, input_message, attempts, row_id = row
                ownership.assert_held(self.db_path, thread_id)
                cursor = await db.execute(
                    "SELECT 1 FROM pending_approvals WHERE turn_id = ? AND status = 'pending' LIMIT 1",
                    (turn_id,),
                )
                if await cursor.fetchone():
                    await db.rollback()
                    return None
                if notify:
                    await db.execute("UPDATE active_turns SET delivery_requested = 1 WHERE turn_id = ?", (turn_id,))
                newer = await db.execute(
                    "SELECT 1 FROM active_turns WHERE thread_id = ? AND rowid > ? LIMIT 1",
                    (thread_id, row_id),
                )
                if await newer.fetchone():
                    await db.execute(
                        """UPDATE active_turns SET status = 'superseded', completed_at = CURRENT_TIMESTAMP,
                               error = 'superseded by a newer turn in this thread' WHERE turn_id = ?""",
                        (turn_id,),
                    )
                elif int(attempts or 0) >= max_attempts:
                    await db.execute(
                        """UPDATE active_turns SET status = 'failed', completed_at = CURRENT_TIMESTAMP,
                               error = 'gave up after ' || ? || ' resume attempt(s)' WHERE turn_id = ?""",
                        (int(attempts or 0), turn_id),
                    )
                else:
                    await db.execute(
                        "UPDATE active_turns SET status = 'resuming', attempts = attempts + 1 WHERE turn_id = ?",
                        (turn_id,),
                    )
                    claimed = {
                        "turn_id": turn_id,
                        "thread_id": thread_id,
                        "input_message": str(input_message or ""),
                        "attempts": int(attempts or 0) + 1,
                    }
                if claimed is None:
                    await queue_result(db, turn_id, None, failed=True)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        if claimed:
            self._record_durable_metric("durable_turns_resumed", 1)
        return claimed

    async def begin_external_effect(
        self,
        *,
        key: str,
        turn_id: str,
        tool: str,
        args: dict | None = None,
        tool_call_id: str = "",
        dedupe_by_key: bool = False,
    ) -> EffectClaim:
        """Claim an effect before dispatch, or reuse an already recorded result.

        Pending intents never expire into permission to retry. Even different
        arguments must not bypass an unresolved effect in the same turn.
        """
        if not key or not turn_id or not tool:
            raise DurableStateError("effect intent requires key, turn and tool")
        token = str(uuid.uuid4())
        args_json = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        fingerprint = hashlib.sha256(args_json.encode()).hexdigest()
        async with self._open_db() as db:
            await self._ensure_table(db)
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """SELECT e.result, i.tool_call_id, i.dedupe_by_key FROM external_effects e
                       LEFT JOIN effect_intents i ON i.idempotency_key = e.idempotency_key
                       WHERE e.idempotency_key = ?""",
                    (key,),
                )
                recorded = await cursor.fetchone()
                if recorded:
                    # Different model call ids may mean two intended identical
                    # purchases, not a retry. Never silently count them as one.
                    if not recorded[2] and recorded[1] and tool_call_id and recorded[1] != tool_call_id:
                        raise EffectUncertainError("identical operation under a new call id; intent review required")
                    await db.commit()
                    return EffectClaim(result=str(recorded[0]))
                cursor = await db.execute(
                    "SELECT status, effect_protocol FROM active_turns WHERE turn_id = ?", (turn_id,)
                )
                turn = await cursor.fetchone()
                if not turn or turn[0] not in {"running", "resuming"}:
                    raise DurableStateError("external effect requires an active durable turn")
                if turn[1] != 1:
                    raise EffectUncertainError("legacy turn lacks intent-protocol proof; reconcile before dispatch")
                cursor = await db.execute(
                    """SELECT 1 FROM effect_intents
                       WHERE idempotency_key = ? OR (status = 'pending'
                           AND (turn_id = ? OR (tool = ? AND args_fingerprint = ?))) LIMIT 1""",
                    (key, turn_id, tool, fingerprint),
                )
                if await cursor.fetchone():
                    raise EffectUncertainError("unresolved external effect; inspect before resuming")
                await db.execute(
                    """INSERT INTO effect_intents
                           (idempotency_key, turn_id, tool, tool_call_id, dedupe_by_key, args_json, args_fingerprint, claim_token, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                    (key, turn_id, tool, tool_call_id, int(dedupe_by_key), args_json, fingerprint, token),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return EffectClaim(token=token)

    async def finish_external_effect(
        self,
        *,
        key: str,
        token: str,
        turn_id: str,
        tool: str,
        result: str,
    ) -> None:
        """Publish an effect result only for the owner of the pending intent."""
        async with self._open_db() as db:
            await self._ensure_table(db)
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """UPDATE effect_intents SET status = 'recorded', completed_at = CURRENT_TIMESTAMP
                       WHERE idempotency_key = ? AND claim_token = ? AND turn_id = ? AND tool = ?
                         AND status = 'pending'""",
                    (key, token, turn_id, tool),
                )
                if cursor.rowcount != 1:
                    raise DurableStateError("effect claim lost; result was not published")
                await db.execute(
                    """INSERT INTO external_effects (idempotency_key, turn_id, tool, result)
                       VALUES (?, ?, ?, ?)""",
                    (key, turn_id, tool, result),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def assert_turn_effects_settled(self, turn_id: str) -> None:
        """Reject automatic continuation when an earlier dispatch is unresolved."""
        async with self._open_db() as db:
            await self._ensure_table(db)
            await self._assert_effects_settled(db, turn_id)

    async def _assert_effects_settled(self, db: aiosqlite.Connection, turn_id: str) -> None:
        cursor = await db.execute(
            "SELECT 1 FROM effect_intents WHERE turn_id = ? AND status = 'pending' LIMIT 1",
            (turn_id,),
        )
        if await cursor.fetchone():
            raise EffectUncertainError("turn contains an unresolved external effect; review required")

    async def get_external_effect(self, key: str) -> str | None:
        """Read a completed result; never mistake an unfinished intent for absent."""
        async with self._open_db() as db:
            await self._ensure_table(db)
            cursor = await db.execute(
                """SELECT e.result, i.status FROM (SELECT ? AS key) k
                   LEFT JOIN external_effects e ON e.idempotency_key = k.key
                   LEFT JOIN effect_intents i ON i.idempotency_key = k.key""",
                (key,),
            )
            result, intent_status = await cursor.fetchone()
        if result is not None:
            return str(result)
        if intent_status is not None:
            raise EffectUncertainError("external effect has no recorded result")
        return None

    async def record_external_effect(self, *, key: str, turn_id: str, tool: str, result: str) -> bool:
        """Record that a side effect happened. Returns False if it already was.

        INSERT OR IGNORE rather than a check-then-write: two concurrent retries of
        the same call must not both conclude they are first.
        """
        if not key:
            return False
        async with self._open_db() as db:
            await self._ensure_table(db)
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO external_effects
                    (idempotency_key, turn_id, tool, result)
                SELECT ?, ?, ?, ? WHERE NOT EXISTS (
                    SELECT 1 FROM effect_intents WHERE idempotency_key = ?
                )
                """,
                (key, turn_id, tool, result, key),
            )
            await db.commit()
            return bool(cursor.rowcount)

    async def list_external_effects(self, turn_id: str) -> list[dict]:
        """Recorded results and unresolved intents; pending is not proof of failure."""
        async with self._open_db() as db:
            await self._ensure_table(db)
            cursor = await db.execute(
                """SELECT e.idempotency_key, e.tool, e.result, e.created_at, 'recorded', '{}'
                   FROM external_effects e WHERE e.turn_id = ?
                   UNION ALL
                   SELECT i.idempotency_key, i.tool, '', i.created_at, i.status, i.args_json
                   FROM effect_intents i WHERE i.turn_id = ? AND i.status = 'pending'
                   ORDER BY 4""",
                (turn_id, turn_id),
            )
            rows = await cursor.fetchall()
        return [
            {
                "idempotency_key": row[0],
                "tool": row[1],
                "result": row[2],
                "created_at": str(row[3]),
                "status": row[4],
                "args": _safe_json(row[5]),
            }
            for row in rows
        ]

    def _pending_approval_from_row(self, row) -> dict | None:
        """Convert a pending_approvals row into a JSON-safe dict."""
        if not row:
            return None
        try:
            args = json.loads(row[5] or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        delegation = None
        # row[12] is delegation_json; older callers may SELECT fewer columns.
        if len(row) > 12 and row[12]:
            try:
                delegation = json.loads(row[12])
            except (json.JSONDecodeError, TypeError):
                delegation = None
        return {
            "approval_id": row[0],
            "turn_id": row[1],
            "thread_id": row[2],
            "tool_call_id": row[3],
            "tool_name": row[4],
            "args": args,
            "status": row[6],
            "requested_at": row[7],
            "decided_at": row[8],
            "decided_by": row[9],
            "decision": row[10],
            "input_message": row[11],
            "delegation": delegation,
        }

    async def create_pending_approval(
        self,
        *,
        turn_id: str,
        thread_id: str,
        tool_call_id: str,
        tool_name: str,
        args: dict,
        delegation: dict | None = None,
    ) -> str:
        """Create or reuse a pending approval for a durable tool call.

        ``delegation`` is set when the approval originates inside a sub-agent:
        it records the parent ``delegate_to_X`` call (name, id, request) so the
        resume can re-run that delegation with the approved call exempted,
        rather than trying to execute the sub-agent's tool at the top level
        (where it isn't registered).
        """
        approval_id = str(uuid.uuid4())
        args_json = json.dumps(args or {}, ensure_ascii=False, default=str)
        delegation_json = json.dumps(delegation, ensure_ascii=False, default=str) if delegation else None

        async with self._open_db() as db:
            await self._ensure_table(db)
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT thread_id, status FROM active_turns WHERE turn_id = ?", (turn_id,)) as cursor:
                turn = await cursor.fetchone()
            if not turn or turn[0] != thread_id or turn[1] not in {"running", "resuming", "waiting_approval"}:
                raise DurableStateError("approval does not belong to an active turn")
            cursor = await db.execute(
                """
                SELECT approval_id FROM pending_approvals
                WHERE turn_id = ? AND tool_call_id = ? AND status = 'pending'
                """,
                (turn_id, tool_call_id),
            )
            row = await cursor.fetchone()
            if row:
                await db.execute(
                    "UPDATE active_turns SET status = 'waiting_approval' WHERE turn_id = ?",
                    (turn_id,),
                )
                await queue_approval(db, turn_id, str(row[0]))
                await db.commit()
                return str(row[0])

            await db.execute(
                """
                INSERT OR IGNORE INTO pending_approvals
                    (approval_id, turn_id, thread_id, tool_call_id, tool_name, args_json, delegation_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (approval_id, turn_id, thread_id, tool_call_id, tool_name, args_json, delegation_json),
            )
            cursor = await db.execute(
                """
                SELECT approval_id FROM pending_approvals
                WHERE turn_id = ? AND tool_call_id = ? AND status = 'pending'
                ORDER BY requested_at DESC
                LIMIT 1
                """,
                (turn_id, tool_call_id),
            )
            inserted = await cursor.fetchone()
            await db.execute(
                "UPDATE active_turns SET status = 'waiting_approval' WHERE turn_id = ?",
                (turn_id,),
            )
            await queue_approval(db, turn_id, str(inserted[0]) if inserted else approval_id)
            await db.commit()

        return str(inserted[0]) if inserted else approval_id

    async def get_pending_approval(self, approval_id: str) -> dict | None:
        """Return an approval request with its durable turn input."""
        async with self._open_db() as db:
            await self._ensure_table(db)
            cursor = await db.execute(
                """
                SELECT
                    p.approval_id,
                    p.turn_id,
                    p.thread_id,
                    p.tool_call_id,
                    p.tool_name,
                    p.args_json,
                    p.status,
                    p.requested_at,
                    p.decided_at,
                    p.decided_by,
                    p.decision,
                    t.input_message,
                    p.delegation_json
                FROM pending_approvals p
                JOIN active_turns t ON t.turn_id = p.turn_id
                WHERE p.approval_id = ?
                """,
                (approval_id,),
            )
            row = await cursor.fetchone()
        return self._pending_approval_from_row(row)

    async def claim_pending_approval(
        self,
        *,
        approval_id: str,
        decision: str,
        decided_by: str = "",
    ) -> dict | None:
        """Atomically claim a pending approval and return its payload."""
        normalized_decision = "approved" if decision == "approved" else "rejected"

        async with self._open_db() as db:
            await self._ensure_table(db)
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT
                    p.approval_id,
                    p.turn_id,
                    p.thread_id,
                    p.tool_call_id,
                    p.tool_name,
                    p.args_json,
                    p.status,
                    p.requested_at,
                    p.decided_at,
                    p.decided_by,
                    p.decision,
                    t.input_message,
                    p.delegation_json,
                    t.status
                FROM pending_approvals p
                JOIN active_turns t ON t.turn_id = p.turn_id
                WHERE p.approval_id = ? AND p.status = 'pending'
                """,
                (approval_id,),
            )
            row = await cursor.fetchone()
            if not row:
                await db.rollback()
                return None

            if row[13] not in {"running", "resuming", "waiting_approval"}:
                await self._expire_pending_approval(db, str(row[1]), approval_id)
                await db.commit()
                return None

            # Expire stale approvals instead of resuming them: a pending row
            # older than the TTL must not fire a (possibly mutating) tool long
            # after the prompt was shown, in a context that no longer holds.
            if _approval_is_stale(row[7]):
                await self._expire_pending_approval(db, str(row[1]), approval_id)
                await db.commit()
                return None

            await db.execute(
                """
                UPDATE pending_approvals
                SET status = ?,
                    decision = ?,
                    decided_by = ?,
                    decided_at = CURRENT_TIMESTAMP
                WHERE approval_id = ? AND status = 'pending'
                """,
                (normalized_decision, normalized_decision, decided_by, approval_id),
            )
            await db.execute(
                "UPDATE active_turns SET status = 'running' WHERE turn_id = ?",
                (row[1],),
            )
            await suppress_approval(db, row[1], approval_id)
            await db.commit()

        claimed = self._pending_approval_from_row(row)
        if claimed is not None:
            claimed["status"] = normalized_decision
            claimed["decision"] = normalized_decision
            claimed["decided_by"] = decided_by
        return claimed

    async def load_turn_messages(self, thread_id: str, turn_id: str) -> list[BaseMessage]:
        """Rebuild persisted history + current durable turn journal."""
        messages = await self.load(thread_id)

        async with self._open_db() as db:
            await self._ensure_table(db)
            cursor = await db.execute(
                "SELECT input_message FROM active_turns WHERE turn_id = ? AND thread_id = ?",
                (turn_id, thread_id),
            )
            turn_row = await cursor.fetchone()
            if not turn_row:
                raise DurableStateError("durable turn missing or thread does not match")

            messages.append(HumanMessage(content=str(turn_row[0])))
            journal_cursor = await db.execute(
                """
                SELECT message_json FROM turn_journal
                WHERE turn_id = ?
                ORDER BY seq ASC
                """,
                (turn_id,),
            )
            journal_rows = await journal_cursor.fetchall()

        for (raw_message,) in journal_rows:
            messages.append(_deserialize_journal_message(raw_message))
        unanswered_tool_calls(messages)
        return messages

    async def finish_turn(self, turn_id: str) -> None:
        """Mark a turn done and remove its ephemeral journal/cache rows."""
        async with self._open_db() as db:
            await self._ensure_table(db)
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT status FROM active_turns WHERE turn_id = ?", (turn_id,)) as cursor:
                row = await cursor.fetchone()
            if row and row[0] == "waiting_approval":
                raise DurableStateError("cannot finish an approval-waiting turn")
            if not row or row[0] not in {"running", "resuming"}:
                await db.rollback()
                return
            await self._assert_effects_settled(db, turn_id)
            await db.execute(
                """
                UPDATE active_turns
                SET status = 'done', completed_at = CURRENT_TIMESTAMP, error = NULL
                WHERE turn_id = ?
                """,
                (turn_id,),
            )
            await queue_result(db, turn_id, None)
            await db.execute("DELETE FROM turn_journal WHERE turn_id = ?", (turn_id,))
            await db.execute("DELETE FROM tool_results WHERE turn_id = ?", (turn_id,))
            await db.commit()

    async def finalize_turn(
        self,
        *,
        thread_id: str,
        messages: list[BaseMessage],
        turn_id: str,
        content: str | None = None,
        failure_reason: str = "",
    ) -> None:
        """Atomically save history, exact response and terminal execution state.

        Omitted content stays unknown, rather than borrowing another turn's
        answer from shared or compacted history.
        """
        trimmed = trim_completed_history(messages, MAX_HISTORY)
        data = json.dumps(
            [_serialize_message(m) for m in trimmed],
            ensure_ascii=False,
        )

        async with self._open_db() as db:
            await self._ensure_table(db)
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT thread_id, status, final_content, error FROM active_turns WHERE turn_id = ?", (turn_id,)
            ) as cursor:
                existing = await cursor.fetchone()
            if not existing or existing[0] != thread_id:
                raise DurableStateError("finalization does not match its durable turn")
            if existing[1] not in {"running", "resuming"}:
                if (
                    existing[1] == ("failed" if failure_reason else "done")
                    and existing[2] == content
                    and (existing[3] or "") == failure_reason
                ):
                    await db.rollback()
                    return
                raise DurableStateError("cannot replace a terminal or approval-waiting result")
            await self._assert_effects_settled(db, turn_id)
            await db.execute(
                """INSERT INTO sessions (thread_id, messages, updated_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(thread_id) DO UPDATE SET
                     messages = excluded.messages,
                     updated_at = excluded.updated_at""",
                (thread_id, data),
            )
            await db.execute(
                """
                UPDATE active_turns
                SET status = ?, completed_at = CURRENT_TIMESTAMP, error = ?,
                    final_content = ?
                WHERE turn_id = ?
                """,
                ("failed" if failure_reason else "done", failure_reason or None, content, turn_id),
            )
            await queue_result(db, turn_id, content, failed=bool(failure_reason))
            await db.execute("DELETE FROM turn_journal WHERE turn_id = ?", (turn_id,))
            await db.execute("DELETE FROM tool_results WHERE turn_id = ?", (turn_id,))
            await db.commit()

        self._index_to_swarm_fts(thread_id, trimmed)

    async def get_turn_outcome(self, turn_id: str) -> InvocationOutcome:
        """Read a consistent execution/approval snapshot without running tools."""
        async with self._open_db() as db:
            await self._ensure_table(db)
            async with db.execute(
                """
                SELECT t.thread_id, t.status, t.final_content, t.error,
                       p.approval_id, p.status, p.requested_at
                FROM active_turns t
                LEFT JOIN pending_approvals p ON p.turn_id = t.turn_id
                WHERE t.turn_id = ?
                """,
                (turn_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        if not rows:
            return InvocationOutcome("unknown", turn_id=turn_id, reason="turn_missing")

        thread_id, state, content, error = rows[0][:4]
        approvals = [row[4:] for row in rows if row[4] is not None]
        pending = [row for row in approvals if row[1] == "pending"]
        decisions = {row[1] for row in approvals}
        status: InvocationStatus = "unknown"
        reason = error or ""
        approval_id = None
        if state in {"running", "resuming"}:
            # Approve/Reject is not completion: the continuation may be live.
            status = "running"
        elif state == "waiting_approval":
            if len(pending) == 1:
                approval_id = pending[0][0]
                status = "expired" if _approval_is_stale(pending[0][2]) else "waiting_approval"
            elif "expired" in decisions:
                status = "expired"
            else:
                reason = "approval_state_inconsistent"
        elif state == "failed":
            status = "expired" if error == "approval_expired" else "failed"
        elif state in {"recovered", "superseded"}:
            status = "interrupted"
        elif state == "done":
            if pending:
                reason = "completed_with_pending_approval"
            elif "rejected" in decisions:
                status = "rejected"
            elif "expired" in decisions:
                status = "expired"
            elif content is not None:
                status = "completed"
            else:
                reason = "terminal_content_missing"
        return InvocationOutcome(
            status,
            content or "",
            thread_id,
            turn_id,
            approval_id,
            reason,
        )

    async def stop_plan_turn(self, turn_id: str, *, thread_id: str, reason: str, ownership: TurnOwnership) -> dict:
        """Close stop-related approvals only after the executor has released ownership.

        Journals, caches and effects are retained. Already completed work keeps
        its outcome; a stop is not a rollback of an external request.
        """
        if reason not in {"plan_cancelled", "plan_expired"}:
            raise ValueError("invalid plan stop reason")
        ownership.assert_held(self.db_path, thread_id)
        async with self._open_db() as db:
            await self._ensure_table(db)
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT status, error, final_content, effect_protocol FROM active_turns WHERE turn_id = ? AND thread_id = ?",
                    (turn_id, thread_id),
                )
                row = await cursor.fetchone()
                if row is None:
                    await db.rollback()
                    return {"review": True, "content": "Turn identity missing; review required."}
                previous, error, content, protocol = row
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM effect_intents WHERE turn_id = ? AND status = 'pending'", (turn_id,)
                )
                unresolved = (await cursor.fetchone())[0]
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM pending_approvals WHERE turn_id = ? AND status = 'pending'",
                    (turn_id,),
                )
                pending_approvals = (await cursor.fetchone())[0]
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM pending_approvals WHERE turn_id = ? AND status IN ('rejected', 'expired')",
                    (turn_id,),
                )
                refusals = (await cursor.fetchone())[0]
                expected_stop = error in {"plan_cancelled", "plan_expired"}
                review = bool(
                    unresolved
                    or protocol != 1
                    or previous not in {"running", "resuming", "waiting_approval", "done", "failed"}
                    or (previous == "failed" and not expected_stop)
                    or (previous == "done" and (not content or pending_approvals))
                )
                decision = "expired" if reason == "plan_expired" else "rejected"
                await db.execute(
                    """UPDATE pending_approvals SET status = ?, decision = ?, decided_by = 'plan_lifecycle',
                           decided_at = CURRENT_TIMESTAMP WHERE turn_id = ? AND status = 'pending'""",
                    (decision, decision, turn_id),
                )
                await db.execute(
                    """UPDATE active_turns SET status = 'failed', error = ?, completed_at = CURRENT_TIMESTAMP
                       WHERE turn_id = ? AND status IN ('running', 'resuming', 'waiting_approval')""",
                    (reason, turn_id),
                )
                if review:
                    # Preserve uncertainty across a crash before the plan DB
                    # acknowledges cleanup, even after pending approvals close.
                    await db.execute(
                        "UPDATE active_turns SET status = 'failed', error = ? WHERE turn_id = ? AND thread_id = ?",
                        (error if error and not expected_stop else "plan_stop_requires_review", turn_id, thread_id),
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return {
            "review": review,
            "completed": previous == "done" and not review and not refusals,
            "content": content or "",
            "pending_effects": int(unresolved),
        }

    async def fail_turn(self, turn_id: str, error: str) -> None:
        """Mark a turn failed after a handled exception."""
        async with self._open_db() as db:
            await self._ensure_table(db)
            await db.execute("BEGIN IMMEDIATE")
            changed = await db.execute(
                """
                UPDATE active_turns
                SET status = 'failed',
                    completed_at = CURRENT_TIMESTAMP,
                    error = ?
                WHERE turn_id = ? AND status IN ('running', 'resuming', 'waiting_approval')
                """,
                (error[:1000], turn_id),
            )
            if changed.rowcount:
                await queue_result(db, turn_id, None, failed=True)
            await db.commit()

    async def recover_abandoned_turns(self) -> int:
        """Report interrupted turns without touching any live conversation."""
        recovered = 0
        for turn in await self.resumable_turns():
            try:
                async with own_conversation(self.db_path, turn["thread_id"], wait=False) as ownership:
                    try:
                        recovered += await self._report_abandoned_turn(turn["turn_id"], ownership)
                    except DurableStateError:
                        await self.fail_turn(turn["turn_id"], "invalid durable journal; review required")
                        log.error("Turn %s requires journal review; history was not overwritten", turn["turn_id"])
            except TurnBusyError:
                continue
        if recovered:
            log.warning("Recovered %d abandoned durable turn(s)", recovered)
            self._record_durable_metric("durable_turns_recovered", recovered)
        return recovered

    async def _report_abandoned_turn(self, turn_id: str, ownership: TurnOwnership) -> int:
        async with self._open_db() as db:
            await self._ensure_table(db)
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """SELECT thread_id, input_message FROM active_turns t
                       WHERE turn_id = ? AND status IN ('running', 'resuming')
                         AND NOT EXISTS (SELECT 1 FROM pending_approvals p
                             WHERE p.turn_id = t.turn_id AND p.status = 'pending')""",
                    (turn_id,),
                )
                row = await cursor.fetchone()
                if not row:
                    await db.rollback()
                    return 0
                thread_id, input_message = row
                ownership.assert_held(self.db_path, thread_id)
                session_cursor = await db.execute(
                    "SELECT messages FROM sessions WHERE thread_id = ?",
                    (thread_id,),
                )
                session_row = await session_cursor.fetchone()
                messages: list[BaseMessage] = []
                if session_row:
                    try:
                        data = json.loads(session_row[0])
                        messages = trim_completed_history([_deserialize_message(d) for d in data], MAX_HISTORY)
                    except (json.JSONDecodeError, KeyError, TypeError) as e:
                        log.warning("Skipping malformed session %s during turn recovery: %s", thread_id, e)
                        messages = []

                messages.append(HumanMessage(content=input_message))

                journal_cursor = await db.execute(
                    """
                    SELECT message_json FROM turn_journal
                    WHERE turn_id = ?
                    ORDER BY seq ASC
                    """,
                    (turn_id,),
                )
                journal_rows = await journal_cursor.fetchall()
                for (raw_message,) in journal_rows:
                    messages.append(_deserialize_journal_message(raw_message))

                for call in unanswered_tool_calls(messages):
                    cursor = await db.execute(
                        """SELECT content FROM tool_results WHERE turn_id = ? AND tool_call_id = ?
                           AND NOT EXISTS (SELECT 1 FROM effect_intents WHERE turn_id = ? AND status = 'pending')""",
                        (turn_id, call["id"], turn_id),
                    )
                    cached = await cursor.fetchone()
                    # Close the provider protocol, not the business operation.
                    # Keep the original journal/cache untouched for reconciliation.
                    content = (
                        str(cached[0])
                        if cached
                        else (
                            "[INTERRUPTED: NO VERIFIED RESULT] The prior execution stopped. "
                            "This is not evidence that the action succeeded or failed. "
                            "Do not repeat the action without reconciling its actual outcome."
                        )
                    )
                    messages.append(ToolMessage(content=content, tool_call_id=call["id"]))

                messages.append(
                    AIMessage(
                        content=(
                            "⚠️ Предыдущий ход был прерван до завершения. "
                            "Я восстановил уже записанные шаги из журнала, "
                            "но не продолжаю его автоматически."
                        ),
                    )
                )
                trimmed = trim_completed_history(messages, MAX_HISTORY)
                data = json.dumps([_serialize_message(m) for m in trimmed], ensure_ascii=False)
                await db.execute(
                    """INSERT INTO sessions (thread_id, messages, updated_at)
                       VALUES (?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(thread_id) DO UPDATE SET
                         messages = excluded.messages,
                         updated_at = excluded.updated_at""",
                    (thread_id, data),
                )
                await db.execute(
                    """
                    UPDATE active_turns
                    SET status = 'recovered',
                        completed_at = CURRENT_TIMESTAMP,
                        error = 'recovered after interrupted turn'
                    WHERE turn_id = ?
                    """,
                    (turn_id,),
                )
                await queue_result(db, turn_id, None, failed=True)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        self._index_to_swarm_fts(thread_id, trimmed)
        return 1

    def _record_durable_metric(self, metric: str, delta: int) -> None:
        """Record durable-turn metrics in swarm_metrics when available."""
        try:
            from kronos.swarm_store import get_swarm

            get_swarm().incr_metric(metric, delta)
        except Exception as e:
            log.debug("Durable metric write failed (non-fatal): %s", e)

    async def load(self, thread_id: str) -> list[BaseMessage]:
        """Load conversation history for a thread."""
        async with self._open_db() as db:
            await self._ensure_table(db)
            cursor = await db.execute(
                "SELECT messages FROM sessions WHERE thread_id = ?",
                (thread_id,),
            )
            row = await cursor.fetchone()

        if not row:
            return []

        try:
            data = json.loads(row[0])
            return trim_completed_history([_deserialize_message(d) for d in data], MAX_HISTORY)
        except (json.JSONDecodeError, KeyError) as e:
            log.error("Failed to deserialize session %s: %s", thread_id, e)
            return []

    async def save(self, thread_id: str, messages: list[BaseMessage]) -> None:
        """Save conversation history, keeping only the last MAX_HISTORY messages."""
        # Trim to max history (keep most recent)
        trimmed = trim_completed_history(messages, MAX_HISTORY)

        data = json.dumps(
            [_serialize_message(m) for m in trimmed],
            ensure_ascii=False,
        )

        async with self._open_db() as db:
            await self._ensure_table(db)
            await db.execute(
                """INSERT INTO sessions (thread_id, messages, updated_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(thread_id) DO UPDATE SET
                     messages = excluded.messages,
                     updated_at = excluded.updated_at""",
                (thread_id, data),
            )
            await db.commit()

        self._index_to_swarm_fts(thread_id, trimmed)

    def _index_to_swarm_fts(
        self,
        thread_id: str,
        messages: list[BaseMessage],
    ) -> int:
        """Index session messages into swarm FTS. Non-blocking, non-fatal."""
        if not self._agent_name:
            return 0
        try:
            from kronos.swarm_store import get_swarm

            swarm = get_swarm()
            indexed = 0
            for position, msg in enumerate(messages):
                if isinstance(msg, HumanMessage):
                    role = "user"
                elif isinstance(msg, AIMessage):
                    role = "assistant"
                else:
                    continue
                if msg.content and isinstance(msg.content, str) and len(msg.content) > 5:
                    inserted = swarm.index_session_message(
                        agent_name=self._agent_name,
                        thread_id=thread_id,
                        role=role,
                        content=msg.content,
                        fingerprint=_session_fts_fingerprint(
                            agent_name=self._agent_name,
                            thread_id=thread_id,
                            position=position,
                            role=role,
                            content=msg.content,
                        ),
                    )
                    if inserted:
                        indexed += 1
            return indexed
        except Exception as e:
            log.warning("FTS indexing failed (non-fatal): %s", e)
            return 0

    async def backfill_swarm_fts(self) -> int:
        """Index existing session rows into the shared session-search FTS store.

        This is idempotent when the target swarm database has fingerprints.
        """
        if not self._agent_name:
            log.info("Skipping session FTS backfill: agent_name is empty")
            return 0

        rows: list[tuple[str, str]] = []
        async with self._open_db() as db:
            await self._ensure_table(db)
            cursor = await db.execute("SELECT thread_id, messages FROM sessions")
            rows = await cursor.fetchall()

        indexed = 0
        for thread_id, raw_messages in rows:
            try:
                data = json.loads(raw_messages)
                messages = [_deserialize_message(d) for d in data]
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                log.warning("Skipping malformed session %s during FTS backfill: %s", thread_id, e)
                continue
            indexed += self._index_to_swarm_fts(thread_id, messages)

        log.info("Session FTS backfill complete: %d new messages indexed", indexed)
        return indexed

    async def clear(self, thread_id: str) -> int:
        """Clear conversation history for a thread. Returns rows deleted."""
        async with self._open_db() as db:
            await self._ensure_table(db)
            # Clear new sessions table
            cursor = await db.execute(
                "DELETE FROM sessions WHERE thread_id = ?",
                (thread_id,),
            )
            deleted = cursor.rowcount

            # Also clear legacy LangGraph checkpoint tables if they exist
            for table in ("checkpoints", "writes"):
                try:
                    cursor = await db.execute(
                        f"DELETE FROM {table} WHERE thread_id = ?",
                        (thread_id,),
                    )
                    deleted += cursor.rowcount
                except Exception:
                    pass  # table may not exist

            await db.commit()
            return deleted
