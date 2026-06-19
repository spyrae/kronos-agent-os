"""Session store — persistent conversation history per thread_id.

Replaces LangGraph's AsyncSqliteSaver checkpointer.
Stores messages as JSON in SQLite, keyed by thread_id.
"""

import hashlib
import json
import logging
import uuid
from contextlib import asynccontextmanager

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
                    decision TEXT
                )
            """)
            await db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_approvals_turn_call
                    ON pending_approvals(turn_id, tool_call_id)
                    WHERE status = 'pending'
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_pending_approvals_status
                    ON pending_approvals(status, requested_at)
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

    def _pending_approval_from_row(self, row) -> dict | None:
        """Convert a pending_approvals row into a JSON-safe dict."""
        if not row:
            return None
        try:
            args = json.loads(row[5] or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
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
        }

    async def create_pending_approval(
        self,
        *,
        turn_id: str,
        thread_id: str,
        tool_call_id: str,
        tool_name: str,
        args: dict,
    ) -> str:
        """Create or reuse a pending approval for a durable tool call."""
        approval_id = str(uuid.uuid4())
        args_json = json.dumps(args or {}, ensure_ascii=False, default=str)

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
                    (approval_id, turn_id, thread_id, tool_call_id, tool_name, args_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (approval_id, turn_id, thread_id, tool_call_id, tool_name, args_json),
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
                    t.input_message
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
                    t.input_message
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

                messages.append(AIMessage(
                    content=(
                        "⚠️ Предыдущий ход был прерван до завершения. "
                        "Я восстановил уже записанные шаги из журнала, "
                        "но не продолжаю его автоматически."
                    ),
                ))
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
        self, thread_id: str, messages: list[BaseMessage],
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
