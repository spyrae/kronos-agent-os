"""Session store — persistent conversation history per thread_id.

Replaces LangGraph's AsyncSqliteSaver checkpointer.
Stores messages as JSON in SQLite, keyed by thread_id.
"""

import hashlib
import json
import logging
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import aiosqlite
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

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
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=30000")
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
            self._initialized = True

    async def begin_turn(self, thread_id: str, input_message: str) -> str:
        """Open a durable turn record and return its id."""
        turn_id = str(uuid.uuid4())
        async with self._open_db() as db:
            await self._ensure_table(db)
            await db.execute(
                """
                INSERT INTO active_turns
                    (turn_id, thread_id, status, input_message)
                VALUES (?, ?, 'running', ?)
                """,
                (turn_id, thread_id, input_message),
            )
            await db.commit()
        return turn_id

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

    async def get_turn_detail(self, turn_id: str) -> dict | None:
        """One turn with its journal, memoized tool results and effects."""
        async with self._open_db() as db:
            await self._ensure_table(db)
            cursor = await db.execute(
                """
                SELECT turn_id, thread_id, status, input_message, attempts, started_at, completed_at, error
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
                WHERE status NOT IN ('running', 'resuming')
                  AND COALESCE(completed_at, started_at) < datetime('now', ?)
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

    async def claim_turns_for_resume(self, *, max_attempts: int = 2) -> list[dict]:
        """Claim interrupted turns for re-execution, newest-first per thread.

        Returns rows the caller should finish. Three outcomes are decided here,
        under one transaction, so two processes cannot both claim a turn:

        * a turn whose thread already saw a newer turn is marked ``superseded`` —
          the user asked again, and answering the stale question would be noise;
        * a turn that has already burned its attempts is failed, so a crash loop
          cannot resurrect itself forever;
        * anything else flips to ``resuming`` and is handed back.
        """
        claimed: list[dict] = []
        async with self._open_db() as db:
            await self._ensure_table(db)
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """
                    SELECT turn_id, thread_id, input_message, attempts, rowid
                    FROM active_turns
                    WHERE status = 'running'
                    ORDER BY rowid ASC
                    """
                )
                rows = await cursor.fetchall()

                for turn_id, thread_id, input_message, attempts, row_id in rows:
                    # Ordering by rowid, not started_at: CURRENT_TIMESTAMP has
                    # second resolution, so two turns in the same second would
                    # both look "not newer" and a stale one would be resumed.
                    newer = await db.execute(
                        """
                        SELECT 1 FROM active_turns
                        WHERE thread_id = ? AND rowid > ?
                        LIMIT 1
                        """,
                        (thread_id, row_id),
                    )
                    if await newer.fetchone():
                        await db.execute(
                            """
                            UPDATE active_turns
                            SET status = 'superseded', completed_at = CURRENT_TIMESTAMP,
                                error = 'superseded by a newer turn in this thread'
                            WHERE turn_id = ?
                            """,
                            (turn_id,),
                        )
                        continue

                    if int(attempts or 0) >= max_attempts:
                        await db.execute(
                            """
                            UPDATE active_turns
                            SET status = 'failed', completed_at = CURRENT_TIMESTAMP,
                                error = 'gave up after ' || ? || ' resume attempt(s)'
                            WHERE turn_id = ?
                            """,
                            (int(attempts or 0), turn_id),
                        )
                        continue

                    await db.execute(
                        "UPDATE active_turns SET status = 'resuming', attempts = attempts + 1 WHERE turn_id = ?",
                        (turn_id,),
                    )
                    claimed.append(
                        {
                            "turn_id": str(turn_id),
                            "thread_id": str(thread_id),
                            "input_message": str(input_message or ""),
                            "attempts": int(attempts or 0) + 1,
                        }
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

        if claimed:
            log.warning("Claimed %d interrupted turn(s) for resume", len(claimed))
            self._record_durable_metric("durable_turns_resumed", len(claimed))
        return claimed

    async def get_external_effect(self, key: str) -> str | None:
        """Return the recorded result of a side-effecting call, if it already ran.

        This is what makes re-running a turn safe: a message that was already
        sent must not be sent twice just because the process died before the
        journal recorded the answer.
        """
        if not key:
            return None
        async with self._open_db() as db:
            await self._ensure_table(db)
            cursor = await db.execute(
                "SELECT result FROM external_effects WHERE idempotency_key = ?",
                (key,),
            )
            row = await cursor.fetchone()
        return str(row[0]) if row else None

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
                VALUES (?, ?, ?, ?)
                """,
                (key, turn_id, tool, result),
            )
            await db.commit()
            return bool(cursor.rowcount)

    async def list_external_effects(self, turn_id: str) -> list[dict]:
        """Effects recorded for one turn (dashboard / debugging)."""
        async with self._open_db() as db:
            await self._ensure_table(db)
            cursor = await db.execute(
                """
                SELECT idempotency_key, tool, result, created_at
                FROM external_effects WHERE turn_id = ? ORDER BY created_at
                """,
                (turn_id,),
            )
            rows = await cursor.fetchall()
        return [
            {"idempotency_key": row[0], "tool": row[1], "result": row[2], "created_at": str(row[3])} for row in rows
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
                    p.delegation_json
                FROM pending_approvals p
                JOIN active_turns t ON t.turn_id = p.turn_id
                WHERE p.approval_id = ? AND p.status = 'pending'
                """,
                (approval_id,),
            )
            row = await cursor.fetchone()
            if not row:
                await db.commit()
                return None

            # Expire stale approvals instead of resuming them: a pending row
            # older than the TTL must not fire a (possibly mutating) tool long
            # after the prompt was shown, in a context that no longer holds.
            if _approval_is_stale(row[7]):
                await db.execute(
                    "UPDATE pending_approvals SET status = 'expired' WHERE approval_id = ? AND status = 'pending'",
                    (approval_id,),
                )
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
                "SELECT input_message FROM active_turns WHERE turn_id = ?",
                (turn_id,),
            )
            turn_row = await cursor.fetchone()
            if not turn_row:
                return messages

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
            try:
                messages.append(_deserialize_message(json.loads(raw_message)))
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                log.warning("Skipping malformed journal message for turn %s: %s", turn_id, e)
        return messages

    async def finish_turn(self, turn_id: str) -> None:
        """Mark a turn done and remove its ephemeral journal/cache rows."""
        async with self._open_db() as db:
            await self._ensure_table(db)
            await db.execute(
                """
                UPDATE active_turns
                SET status = 'done', completed_at = CURRENT_TIMESTAMP, error = NULL
                WHERE turn_id = ?
                """,
                (turn_id,),
            )
            await db.execute("DELETE FROM turn_journal WHERE turn_id = ?", (turn_id,))
            await db.execute("DELETE FROM tool_results WHERE turn_id = ?", (turn_id,))
            await db.commit()

    async def finalize_turn(
        self,
        *,
        thread_id: str,
        messages: list[BaseMessage],
        turn_id: str,
    ) -> None:
        """Atomically save session history and close a durable turn."""
        trimmed = messages[-MAX_HISTORY:] if len(messages) > MAX_HISTORY else messages
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
            await db.execute(
                """
                UPDATE active_turns
                SET status = 'done', completed_at = CURRENT_TIMESTAMP, error = NULL
                WHERE turn_id = ?
                """,
                (turn_id,),
            )
            await db.execute("DELETE FROM turn_journal WHERE turn_id = ?", (turn_id,))
            await db.execute("DELETE FROM tool_results WHERE turn_id = ?", (turn_id,))
            await db.commit()

        self._index_to_swarm_fts(thread_id, trimmed)

    async def fail_turn(self, turn_id: str, error: str) -> None:
        """Mark a turn failed after a handled exception."""
        async with self._open_db() as db:
            await self._ensure_table(db)
            await db.execute(
                """
                UPDATE active_turns
                SET status = 'failed',
                    completed_at = CURRENT_TIMESTAMP,
                    error = ?
                WHERE turn_id = ?
                """,
                (error[:1000], turn_id),
            )
            await db.commit()

    async def recover_abandoned_turns(self) -> int:
        """Recover running turns left behind by a crashed/restarted process.

        MVP behavior is recover-and-report: append the input, journaled
        assistant/tool deltas, and an interruption notice to the persisted
        session. It does not resume tool execution.
        """
        recovered_sessions: list[tuple[str, list[BaseMessage]]] = []
        async with self._open_db() as db:
            await self._ensure_table(db)
            cursor = await db.execute(
                """
                SELECT turn_id, thread_id, input_message
                FROM active_turns
                WHERE status = 'running'
                ORDER BY started_at ASC
                """
            )
            active_rows = await cursor.fetchall()

            for turn_id, thread_id, input_message in active_rows:
                session_cursor = await db.execute(
                    "SELECT messages FROM sessions WHERE thread_id = ?",
                    (thread_id,),
                )
                session_row = await session_cursor.fetchone()
                messages: list[BaseMessage] = []
                if session_row:
                    try:
                        data = json.loads(session_row[0])
                        messages = [_deserialize_message(d) for d in data]
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
                    try:
                        messages.append(_deserialize_message(json.loads(raw_message)))
                    except (json.JSONDecodeError, KeyError, TypeError) as e:
                        log.warning("Skipping malformed journal message for turn %s: %s", turn_id, e)

                messages.append(
                    AIMessage(
                        content=(
                            "⚠️ Предыдущий ход был прерван до завершения. "
                            "Я восстановил уже записанные шаги из журнала, "
                            "но не продолжаю его автоматически."
                        ),
                    )
                )
                trimmed = messages[-MAX_HISTORY:] if len(messages) > MAX_HISTORY else messages
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
                recovered_sessions.append((thread_id, trimmed))

            await db.commit()

        for thread_id, messages in recovered_sessions:
            self._index_to_swarm_fts(thread_id, messages)

        recovered = len(recovered_sessions)
        if recovered:
            log.warning("Recovered %d abandoned durable turn(s)", recovered)
            self._record_durable_metric("durable_turns_recovered", recovered)
        return recovered

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
            return [_deserialize_message(d) for d in data]
        except (json.JSONDecodeError, KeyError) as e:
            log.error("Failed to deserialize session %s: %s", thread_id, e)
            return []

    async def save(self, thread_id: str, messages: list[BaseMessage]) -> None:
        """Save conversation history, keeping only the last MAX_HISTORY messages."""
        # Trim to max history (keep most recent)
        trimmed = messages[-MAX_HISTORY:] if len(messages) > MAX_HISTORY else messages

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
