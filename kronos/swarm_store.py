"""Shared cross-agent ledger — one SQLite file, six concurrent writers.

This is the shared-state substrate that lets the 6 agent processes
coordinate without a pub/sub bus. Two tables:

``swarm_messages``
    Every message observed in a group chat (user, agent, or system). Used
    for cross-agent visibility, debugging, retention analysis, and root-
    message lookup by the router.

``reply_claims``
    Coordination ledger. An agent inserts a ``claimed`` row when it decides
    to reply to a message. Before actually sending, it runs an IMMEDIATE
    transaction to check that it is still the winner (lowest tier, earliest
    eta); if so it flips to ``sent``, otherwise it cancels. This replaces
    the pub/sub bus we considered in Phase 3 of the original plan.

Winner rule: ``ORDER BY tier ASC, eta_ts ASC, agent_name ASC``.

Access goes through :class:`SwarmStore`, a thin facade over the ``SafeDB``
helper (WAL mode, single-connection-with-lock, auto-reconnect). The
underlying file lives at ``settings.swarm_db_path``.

Metrics counters (``addressing_violations``, ``duplicate_replies``) are
written here too — this is the natural spot because every agent sees the
same ledger and can agree on "who did what".
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass

from kronos.db import get_db

log = logging.getLogger("kronos.swarm")


# Feedback emoji classification
POSITIVE_EMOJI = {"👍", "❤️", "🔥", "🎉", "💯", "⚡", "🏆", "👏", "❤"}
NEGATIVE_EMOJI = {"👎", "💩", "🤮", "😡"}

# Claim states
CLAIM_STATE_CLAIMED = "claimed"
CLAIM_STATE_SENT = "sent"
CLAIM_STATE_CANCELLED = "cancelled"
CLAIM_STATE_EXPIRED = "expired"

# A ``claimed`` row older than this many seconds (arbitration won but the
# agent never started executing) is considered stale — the agent crashed
# between claiming and winning. Lazy cleanup.
CLAIM_EXPIRY_SECONDS = 120

# Once an agent wins arbitration it flips its claim to ``executing`` and holds
# that lease while the (possibly slow) LLM/tool run and Telegram delivery
# happen. The lease is deliberately generous so a long invoke is NOT mistaken
# for a dead agent and answered a second time by a peer. If the process really
# dies mid-run, the lease still expires and another agent may pick up.
EXECUTING_LEASE_SECONDS = 600

# Retention for swarm_messages (used by a cron job, not enforced here).
MESSAGE_RETENTION_DAYS = 90

# Hard cap on non-explicit substantive replies to one root user message.
# Applies to Tier 2 and Tier 3; Tier 1 is exempt (explicit addressing wins).
DEFAULT_MAX_IMPLICIT_REPLIES = 2


def _schema(conn) -> None:
    # Migrate BEFORE the main script: on a pre-fingerprint session_messages
    # table the script's fingerprint index aborts executescript mid-way, so
    # every statement after it (incl. the swarm 2.0 tables) is never created
    # and get_swarm() raises on startup.
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(session_messages)").fetchall()
    }
    if columns and "fingerprint" not in columns:
        conn.execute("ALTER TABLE session_messages ADD COLUMN fingerprint TEXT")

    # reply_claims gained an 'executing' state (a lease held between winning
    # arbitration and confirmed delivery). SQLite can't ALTER a CHECK
    # constraint, so recreate the table when the legacy constraint is present.
    # reply_claims is an ephemeral ledger (claims live minutes), so copying
    # rows forward is safe. Runs before the main script below, whose
    # CREATE TABLE / CREATE INDEX IF NOT EXISTS then no-op / rebuild indexes.
    claims_ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='reply_claims'"
    ).fetchone()
    if claims_ddl and claims_ddl[0] and "'executing'" not in claims_ddl[0]:
        conn.executescript(
            """
            ALTER TABLE reply_claims RENAME TO reply_claims_legacy;
            CREATE TABLE reply_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL DEFAULT 0,
                root_msg_id INTEGER NOT NULL,
                trigger_msg_id INTEGER NOT NULL,
                agent_name TEXT NOT NULL,
                tier INTEGER NOT NULL,
                eta_ts REAL NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('claimed','executing','sent','cancelled','expired')
                ),
                reason TEXT,
                reply_msg_id INTEGER,
                created_at REAL NOT NULL,
                UNIQUE (chat_id, topic_id, trigger_msg_id, agent_name)
            );
            INSERT INTO reply_claims
                (id, chat_id, topic_id, root_msg_id, trigger_msg_id, agent_name,
                 tier, eta_ts, state, reason, reply_msg_id, created_at)
                SELECT id, chat_id, topic_id, root_msg_id, trigger_msg_id,
                       agent_name, tier, eta_ts, state, reason, reply_msg_id,
                       created_at
                FROM reply_claims_legacy;
            DROP TABLE reply_claims_legacy;
            """
        )

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS swarm_messages (
            chat_id INTEGER NOT NULL,
            topic_id INTEGER NOT NULL DEFAULT 0,
            msg_id INTEGER NOT NULL,
            reply_to_msg_id INTEGER,
            sender_id INTEGER NOT NULL,
            sender_type TEXT NOT NULL CHECK (sender_type IN ('user', 'agent', 'system')),
            agent_name TEXT,
            text TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (chat_id, topic_id, msg_id)
        );
        CREATE INDEX IF NOT EXISTS idx_swarm_messages_recent
            ON swarm_messages(chat_id, topic_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_swarm_messages_replies
            ON swarm_messages(chat_id, topic_id, reply_to_msg_id);
        CREATE INDEX IF NOT EXISTS idx_swarm_messages_agent
            ON swarm_messages(agent_name, created_at DESC);

        CREATE TABLE IF NOT EXISTS reply_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            topic_id INTEGER NOT NULL DEFAULT 0,
            root_msg_id INTEGER NOT NULL,
            trigger_msg_id INTEGER NOT NULL,
            agent_name TEXT NOT NULL,
            tier INTEGER NOT NULL,
            eta_ts REAL NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('claimed','executing','sent','cancelled','expired')),
            reason TEXT,
            reply_msg_id INTEGER,
            created_at REAL NOT NULL,
            UNIQUE (chat_id, topic_id, trigger_msg_id, agent_name)
        );
        CREATE INDEX IF NOT EXISTS idx_reply_claims_active
            ON reply_claims(chat_id, topic_id, root_msg_id, state);
        CREATE INDEX IF NOT EXISTS idx_reply_claims_winner
            ON reply_claims(chat_id, topic_id, root_msg_id, tier, eta_ts, agent_name);

        CREATE TABLE IF NOT EXISTS swarm_metrics (
            metric TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL
        );

        -- Shared user facts: one view of the user for all agents.
        -- Classification rule (v1 heuristic): facts derived from USER messages
        -- land here; facts derived from the agent's own reflections stay in
        -- the per-agent Mem0 collection. No LLM classifier in v1.
        CREATE TABLE IF NOT EXISTS shared_user_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            fact TEXT NOT NULL,
            source_agent TEXT NOT NULL,
            created_at REAL NOT NULL,
            last_accessed_at REAL NOT NULL,
            access_count INTEGER NOT NULL DEFAULT 0,
            UNIQUE (user_id, fact)
        );
        CREATE INDEX IF NOT EXISTS idx_shared_user_facts_user
            ON shared_user_facts(user_id, last_accessed_at DESC);

        -- FTS5 keyword index over facts. Uses external content to stay in
        -- sync via triggers; falls back to raw storage if FTS5 unavailable.
        CREATE VIRTUAL TABLE IF NOT EXISTS shared_user_facts_fts
            USING fts5(fact, content='shared_user_facts', content_rowid='id');

        CREATE TRIGGER IF NOT EXISTS shared_user_facts_ai
            AFTER INSERT ON shared_user_facts BEGIN
                INSERT INTO shared_user_facts_fts(rowid, fact)
                VALUES (new.id, new.fact);
            END;
        CREATE TRIGGER IF NOT EXISTS shared_user_facts_ad
            AFTER DELETE ON shared_user_facts BEGIN
                INSERT INTO shared_user_facts_fts(shared_user_facts_fts, rowid, fact)
                VALUES ('delete', old.id, old.fact);
            END;
        CREATE TRIGGER IF NOT EXISTS shared_user_facts_au
            AFTER UPDATE ON shared_user_facts BEGIN
                INSERT INTO shared_user_facts_fts(shared_user_facts_fts, rowid, fact)
                VALUES ('delete', old.id, old.fact);
                INSERT INTO shared_user_facts_fts(rowid, fact)
                VALUES (new.id, new.fact);
            END;

        -- Session messages: cross-agent FTS5 search over conversation history.
        CREATE TABLE IF NOT EXISTS session_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL,
            fingerprint TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_session_messages_thread
            ON session_messages(agent_name, thread_id, created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_session_messages_fingerprint
            ON session_messages(fingerprint)
            WHERE fingerprint IS NOT NULL;

        CREATE VIRTUAL TABLE IF NOT EXISTS session_messages_fts
            USING fts5(content, tokenize='unicode61', content='session_messages', content_rowid='id');

        CREATE TRIGGER IF NOT EXISTS session_messages_fts_ai
            AFTER INSERT ON session_messages BEGIN
                INSERT INTO session_messages_fts(rowid, content)
                VALUES (new.id, new.content);
            END;
        CREATE TRIGGER IF NOT EXISTS session_messages_fts_ad
            AFTER DELETE ON session_messages BEGIN
                INSERT INTO session_messages_fts(session_messages_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
            END;

        -- Feedback: Telegram reactions → RL signal for self-improvement.
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            msg_id INTEGER NOT NULL,
            reaction TEXT NOT NULL,
            emoji TEXT NOT NULL,
            created_at REAL NOT NULL,
            UNIQUE(chat_id, msg_id, agent_name)
        );
        CREATE INDEX IF NOT EXISTS idx_feedback_agent
            ON feedback(agent_name, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_feedback_reaction
            ON feedback(reaction, created_at DESC);

        -- Cross-agent hand-off queue (roadmap 5.1): agent A routes a request it
        -- deems out of its domain to profile agent B, who polls its pending
        -- rows and answers — instead of A going silent or replying worse.
        CREATE TABLE IF NOT EXISTS handoffs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            topic_id INTEGER NOT NULL DEFAULT 0,
            thread_id TEXT NOT NULL,
            from_agent TEXT NOT NULL,
            to_agent TEXT NOT NULL,
            context TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('pending','accepted','done','failed')),
            created_at REAL NOT NULL,
            accepted_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_handoffs_intake
            ON handoffs(to_agent, state, created_at);

        -- Council sessions (roadmap 5.2): a structured multi-agent debate. The
        -- initiator convenes N participants who each submit an independent
        -- position; once all are in, the initiator synthesizes one answer.
        CREATE TABLE IF NOT EXISTS council_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            topic_id INTEGER NOT NULL DEFAULT 0,
            thread_id TEXT NOT NULL,
            initiator TEXT NOT NULL,
            question TEXT NOT NULL,
            participants TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('gathering','synthesizing','done','failed')),
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_council_state
            ON council_sessions(state, initiator);

        CREATE TABLE IF NOT EXISTS council_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            agent_name TEXT NOT NULL,
            position TEXT NOT NULL,
            created_at REAL NOT NULL,
            UNIQUE (session_id, agent_name)
        );

        -- Cross-agent memory queries (roadmap 5.3): each agent has a private
        -- Mem0/FTS, so one agent asks another "what do you have on X" and the
        -- target shares from its own memory. Fire-and-forget into the chat.
        CREATE TABLE IF NOT EXISTS memory_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            topic_id INTEGER NOT NULL DEFAULT 0,
            thread_id TEXT NOT NULL,
            from_agent TEXT NOT NULL,
            to_agent TEXT NOT NULL,
            query TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('pending','done','failed')),
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memory_requests_intake
            ON memory_requests(to_agent, state, created_at);

        -- Per-day, per-agent cost ledger. All six agents write here so the
        -- daily budget is swarm-wide: one agent can't quietly burn the whole
        -- limit on its own while the other five each read a private total.
        -- UPSERT-incremented per LLM call by the cost-tracking callback and
        -- read by CostGuardian for the daily cap.
        CREATE TABLE IF NOT EXISTS swarm_costs (
            day TEXT NOT NULL,
            agent TEXT NOT NULL,
            requests INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL,
            PRIMARY KEY (day, agent)
        );
        """
    )


@dataclass
class ClaimOutcome:
    """Result of attempting to claim / check a reply slot."""

    won: bool
    reason: str = ""


class SwarmStore:
    """Facade over the shared swarm ledger."""

    def __init__(self):
        self._db = get_db("swarm")
        self._db.init_schema(_schema)

    # ------------------------------------------------------------------
    # swarm_messages
    # ------------------------------------------------------------------

    def record_inbound_message(
        self,
        *,
        chat_id: int,
        topic_id: int | None,
        msg_id: int,
        reply_to_msg_id: int | None,
        sender_id: int,
        sender_type: str,
        agent_name: str | None,
        text: str,
    ) -> None:
        """Record any observed message. Idempotent per PRIMARY KEY."""
        self._db.write(
            """
            INSERT OR IGNORE INTO swarm_messages
                (chat_id, topic_id, msg_id, reply_to_msg_id,
                 sender_id, sender_type, agent_name, text, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                topic_id or 0,
                msg_id,
                reply_to_msg_id,
                sender_id,
                sender_type,
                agent_name,
                text,
                time.time(),
            ),
        )

    def record_outbound_message(
        self,
        *,
        chat_id: int,
        topic_id: int | None,
        msg_id: int,
        reply_to_msg_id: int | None,
        agent_name: str,
        text: str,
    ) -> None:
        """Record a message this agent just sent. Stable via INSERT OR IGNORE."""
        # We fabricate a sender_id of -1 for agent rows we posted ourselves
        # because Telethon only yields the proper bot sender_id on next poll.
        self._db.write(
            """
            INSERT OR IGNORE INTO swarm_messages
                (chat_id, topic_id, msg_id, reply_to_msg_id,
                 sender_id, sender_type, agent_name, text, created_at)
            VALUES (?, ?, ?, ?, ?, 'agent', ?, ?, ?)
            """,
            (
                chat_id,
                topic_id or 0,
                msg_id,
                reply_to_msg_id,
                -1,
                agent_name,
                text,
                time.time(),
            ),
        )

    def get_recent_messages(
        self,
        *,
        chat_id: int,
        topic_id: int | None,
        limit: int = 20,
    ) -> list[dict]:
        """Return most recent messages in chat/topic, newest first."""
        rows = self._db.read(
            """
            SELECT msg_id, reply_to_msg_id, sender_id, sender_type,
                   agent_name, text, created_at
            FROM swarm_messages
            WHERE chat_id = ? AND topic_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (chat_id, topic_id or 0, limit),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # reply_claims — coordination
    # ------------------------------------------------------------------

    def claim_reply(
        self,
        *,
        chat_id: int,
        topic_id: int | None,
        root_msg_id: int,
        trigger_msg_id: int,
        agent_name: str,
        tier: int,
        eta_ts: float,
        reason: str = "",
    ) -> None:
        """Insert a claim row. Idempotent per (trigger_msg_id, agent_name)."""
        self._db.write(
            """
            INSERT OR IGNORE INTO reply_claims
                (chat_id, topic_id, root_msg_id, trigger_msg_id,
                 agent_name, tier, eta_ts, state, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'claimed', ?, ?)
            """,
            (
                chat_id,
                topic_id or 0,
                root_msg_id,
                trigger_msg_id,
                agent_name,
                tier,
                eta_ts,
                reason,
                time.time(),
            ),
        )

    def can_send_claim(
        self,
        *,
        chat_id: int,
        topic_id: int | None,
        root_msg_id: int,
        agent_name: str,
        tier: int,
        max_implicit_replies: int = DEFAULT_MAX_IMPLICIT_REPLIES,
    ) -> ClaimOutcome:
        """Atomically decide whether this agent may send now.

        Runs under an IMMEDIATE transaction so that when two agents race to
        check/mark_sent at the same moment, SQLite serialises them and
        exactly one wins.

        Rules:
          * Tier 1 (explicit address) always wins — no cap, no peer check.
          * Otherwise, count ``sent`` replies to this root_msg_id across all
            agents and tiers; reject if already at cap.
          * Otherwise, confirm this agent's claim is still the winner under
            ``ORDER BY tier ASC, eta_ts ASC, agent_name ASC`` among active
            ``claimed`` rows for the same root_msg_id.
        """
        now = time.time()

        def _tx(conn):
            # Lazy-expire stale rows: a ``claimed`` row past CLAIM_EXPIRY (won
            # nothing yet) and an ``executing`` lease past EXECUTING_LEASE (the
            # agent likely died mid-run) both become ``expired``.
            conn.execute(
                """
                UPDATE reply_claims
                SET state = 'expired'
                WHERE (state = 'claimed' AND (? - created_at) > ?)
                   OR (state = 'executing' AND (? - created_at) > ?)
                """,
                (now, CLAIM_EXPIRY_SECONDS, now, EXECUTING_LEASE_SECONDS),
            )

            # Tier 1 bypasses arbitration & cap.
            if tier == 1:
                return ClaimOutcome(True, "tier-1 explicit")

            # Anti-flood cap across all agents. An ``executing`` winner already
            # occupies a reply slot, so it counts alongside ``sent``.
            (sent_count,) = conn.execute(
                """
                SELECT COUNT(*) FROM reply_claims
                WHERE chat_id = ? AND topic_id = ? AND root_msg_id = ?
                  AND state IN ('executing', 'sent') AND tier > 1
                """,
                (chat_id, topic_id or 0, root_msg_id),
            ).fetchone()
            if sent_count >= max_implicit_replies:
                return ClaimOutcome(False, f"cap reached ({sent_count}>={max_implicit_replies})")

            # Winner lookup across still-active rows (claimed OR executing). An
            # agent already ``executing`` outranks any fresh ``claimed`` peer
            # regardless of eta — it already holds the slot and is running — so
            # a late arrival with an earlier eta cannot steal a live invoke.
            winner = conn.execute(
                """
                SELECT agent_name FROM reply_claims
                WHERE chat_id = ? AND topic_id = ? AND root_msg_id = ?
                  AND state IN ('claimed', 'executing')
                ORDER BY (state = 'executing') DESC,
                         tier ASC, eta_ts ASC, agent_name ASC
                LIMIT 1
                """,
                (chat_id, topic_id or 0, root_msg_id),
            ).fetchone()
            if winner is None:
                return ClaimOutcome(False, "no active claim")
            if winner[0] != agent_name:
                return ClaimOutcome(False, f"lost to {winner[0]}")
            return ClaimOutcome(True, "winner")

        return self._db.write_tx(_tx)

    def begin_executing(
        self,
        *,
        chat_id: int,
        topic_id: int | None,
        trigger_msg_id: int,
        agent_name: str,
    ) -> bool:
        """Acquire the executing lease immediately before a (slow) invoke.

        Flips this agent's winning claim from ``claimed`` to ``executing`` and
        renews created_at, so the EXECUTING_LEASE window starts now. This is
        what stops a peer from seeing the 120s ``claimed`` expiry mid-invoke,
        assuming the agent is dead, and answering the same message twice.

        Kept separate from ``can_send_claim`` so the lease covers only the
        invoke+delivery — not the media pre-processing between them, whose
        failure paths should fall back to the shorter ``claimed`` expiry.

        Returns True if the lease was acquired; False means the claim was no
        longer ``claimed`` (already sent/cancelled/expired).
        """
        now = time.time()

        def _tx(conn):
            cur = conn.execute(
                """
                UPDATE reply_claims
                SET state = 'executing', created_at = ?
                WHERE chat_id = ? AND topic_id = ?
                  AND trigger_msg_id = ? AND agent_name = ?
                  AND state = 'claimed'
                """,
                (now, chat_id, topic_id or 0, trigger_msg_id, agent_name),
            )
            return cur.rowcount > 0

        return self._db.write_tx(_tx)

    def mark_sent(
        self,
        *,
        chat_id: int,
        topic_id: int | None,
        trigger_msg_id: int,
        agent_name: str,
        reply_msg_id: int | None,
    ) -> None:
        # Compare-and-set: only an in-flight claim (owner/tier-1 stay 'claimed';
        # a tier-2/3 winner is 'executing') may transition to 'sent'. This never
        # resurrects a row that was already cancelled or expired.
        self._db.write(
            """
            UPDATE reply_claims
            SET state = 'sent', reply_msg_id = ?
            WHERE chat_id = ? AND topic_id = ?
              AND trigger_msg_id = ? AND agent_name = ?
              AND state IN ('claimed', 'executing')
            """,
            (reply_msg_id, chat_id, topic_id or 0, trigger_msg_id, agent_name),
        )

    def cancel_claim(
        self,
        *,
        chat_id: int,
        topic_id: int | None,
        trigger_msg_id: int,
        agent_name: str,
        reason: str = "",
    ) -> None:
        self._db.write(
            """
            UPDATE reply_claims
            SET state = 'cancelled', reason = COALESCE(NULLIF(?, ''), reason)
            WHERE chat_id = ? AND topic_id = ?
              AND trigger_msg_id = ? AND agent_name = ?
              AND state IN ('claimed', 'executing')
            """,
            (reason, chat_id, topic_id or 0, trigger_msg_id, agent_name),
        )

    def count_sent_replies(
        self,
        *,
        chat_id: int,
        topic_id: int | None,
        root_msg_id: int,
    ) -> int:
        row = self._db.read_one(
            """
            SELECT COUNT(*) AS c FROM reply_claims
            WHERE chat_id = ? AND topic_id = ? AND root_msg_id = ?
              AND state = 'sent'
            """,
            (chat_id, topic_id or 0, root_msg_id),
        )
        return int(row["c"]) if row else 0

    # ------------------------------------------------------------------
    # Cross-agent hand-offs (roadmap 5.1)
    # ------------------------------------------------------------------

    def create_handoff(
        self,
        *,
        chat_id: int,
        topic_id: int | None,
        thread_id: str,
        from_agent: str,
        to_agent: str,
        context: str,
    ) -> int:
        """Queue a hand-off from one agent to another. Returns its id."""
        cursor = self._db.write(
            """
            INSERT INTO handoffs
                (chat_id, topic_id, thread_id, from_agent, to_agent,
                 context, state, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (chat_id, topic_id or 0, thread_id, from_agent, to_agent, context, time.time()),
        )
        return int(cursor.lastrowid)

    def accept_next_handoff(self, to_agent: str) -> dict | None:
        """Atomically claim the oldest pending hand-off for this agent.

        Runs under an IMMEDIATE transaction so overlapping intake polls never
        process the same row twice. Returns the row as a dict, or None.
        """

        def _tx(conn):
            row = conn.execute(
                """
                SELECT * FROM handoffs
                WHERE to_agent = ? AND state = 'pending'
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (to_agent,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE handoffs SET state = 'accepted', accepted_at = ? WHERE id = ?",
                (time.time(), row["id"]),
            )
            return dict(row)

        return self._db.write_tx(_tx)

    def complete_handoff(self, handoff_id: int, *, success: bool = True) -> None:
        self._db.write(
            "UPDATE handoffs SET state = ? WHERE id = ?",
            ("done" if success else "failed", handoff_id),
        )

    def pending_handoffs(self, to_agent: str) -> list[dict]:
        rows = self._db.read(
            "SELECT * FROM handoffs WHERE to_agent = ? AND state = 'pending' "
            "ORDER BY created_at",
            (to_agent,),
        )
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Council sessions (roadmap 5.2)
    # ------------------------------------------------------------------

    def create_council(
        self,
        *,
        chat_id: int,
        topic_id: int | None,
        thread_id: str,
        initiator: str,
        question: str,
        participants: list[str],
    ) -> int:
        """Open a council gathering positions from participants. Returns its id."""
        cursor = self._db.write(
            """
            INSERT INTO council_sessions
                (chat_id, topic_id, thread_id, initiator, question,
                 participants, state, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'gathering', ?)
            """,
            (
                chat_id, topic_id or 0, thread_id, initiator, question,
                ",".join(participants), time.time(),
            ),
        )
        return int(cursor.lastrowid)

    def pending_council_tasks(self, agent_name: str) -> list[dict]:
        """Gathering sessions where this agent is a participant but hasn't
        submitted a position yet."""
        rows = self._db.read(
            "SELECT * FROM council_sessions WHERE state = 'gathering' ORDER BY created_at"
        )
        result = []
        for row in rows:
            participants = [p for p in row["participants"].split(",") if p]
            if agent_name not in participants:
                continue
            already = self._db.read_one(
                "SELECT 1 FROM council_positions WHERE session_id = ? AND agent_name = ?",
                (row["id"], agent_name),
            )
            if already is None:
                result.append(dict(row))
        return result

    def submit_position(self, session_id: int, agent_name: str, position: str) -> None:
        """Record an agent's independent position. Idempotent per participant."""
        self._db.write(
            "INSERT OR IGNORE INTO council_positions "
            "(session_id, agent_name, position, created_at) VALUES (?, ?, ?, ?)",
            (session_id, agent_name, position, time.time()),
        )

    def councils_awaiting_synthesis(self, initiator: str) -> list[dict]:
        """Gathering sessions initiated by this agent (synthesis poll targets)."""
        rows = self._db.read(
            "SELECT * FROM council_sessions "
            "WHERE state = 'gathering' AND initiator = ? ORDER BY created_at",
            (initiator,),
        )
        return [dict(row) for row in rows]

    def claim_synthesis(self, session_id: int, initiator: str) -> dict | None:
        """If all participants have submitted and the session is still gathering,
        atomically flip it to 'synthesizing' and return it. Otherwise None.

        Runs under an IMMEDIATE transaction so only one synthesis fires.
        """

        def _tx(conn):
            row = conn.execute(
                "SELECT * FROM council_sessions "
                "WHERE id = ? AND initiator = ? AND state = 'gathering'",
                (session_id, initiator),
            ).fetchone()
            if row is None:
                return None
            participants = [p for p in row["participants"].split(",") if p]
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM council_positions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if count < len(participants):
                return None  # still gathering
            conn.execute(
                "UPDATE council_sessions SET state = 'synthesizing' WHERE id = ?",
                (session_id,),
            )
            return dict(row)

        return self._db.write_tx(_tx)

    def get_positions(self, session_id: int) -> list[dict]:
        rows = self._db.read(
            "SELECT agent_name, position FROM council_positions "
            "WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        )
        return [dict(row) for row in rows]

    def complete_council(self, session_id: int, *, success: bool = True) -> None:
        self._db.write(
            "UPDATE council_sessions SET state = ? WHERE id = ?",
            ("done" if success else "failed", session_id),
        )

    # ------------------------------------------------------------------
    # Cross-agent memory queries (roadmap 5.3)
    # ------------------------------------------------------------------

    def create_memory_request(
        self,
        *,
        chat_id: int,
        topic_id: int | None,
        thread_id: str,
        from_agent: str,
        to_agent: str,
        query: str,
    ) -> int:
        """Queue a memory query for another agent. Returns its id."""
        cursor = self._db.write(
            """
            INSERT INTO memory_requests
                (chat_id, topic_id, thread_id, from_agent, to_agent,
                 query, state, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (chat_id, topic_id or 0, thread_id, from_agent, to_agent, query, time.time()),
        )
        return int(cursor.lastrowid)

    def accept_next_memory_request(self, to_agent: str) -> dict | None:
        """Atomically claim the oldest pending memory query for this agent."""

        def _tx(conn):
            row = conn.execute(
                """
                SELECT * FROM memory_requests
                WHERE to_agent = ? AND state = 'pending'
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (to_agent,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE memory_requests SET state = 'done' WHERE id = ?",
                (row["id"],),
            )
            return dict(row)

        return self._db.write_tx(_tx)

    def complete_memory_request(self, request_id: int, *, success: bool = True) -> None:
        self._db.write(
            "UPDATE memory_requests SET state = ? WHERE id = ?",
            ("done" if success else "failed", request_id),
        )

    def pending_memory_requests(self, to_agent: str) -> list[dict]:
        rows = self._db.read(
            "SELECT * FROM memory_requests WHERE to_agent = ? AND state = 'pending' "
            "ORDER BY created_at",
            (to_agent,),
        )
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def incr_metric(self, metric: str, delta: int = 1) -> None:
        self._db.write(
            """
            INSERT INTO swarm_metrics (metric, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(metric) DO UPDATE
                SET value = value + excluded.value,
                    updated_at = excluded.updated_at
            """,
            (metric, delta, time.time()),
        )

    def get_metrics(self) -> dict[str, int]:
        rows = self._db.read("SELECT metric, value FROM swarm_metrics")
        return {r["metric"]: int(r["value"]) for r in rows}

    # ------------------------------------------------------------------
    # Cost ledger — swarm-wide daily budget (shared across all agents)
    # ------------------------------------------------------------------

    def add_cost(
        self,
        *,
        agent: str,
        cost_usd: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
        day: str = "",
    ) -> None:
        """Add one LLM call's cost to the shared per-day, per-agent ledger."""
        bucket = day or time.strftime("%Y-%m-%d")
        self._db.write(
            """
            INSERT INTO swarm_costs
                (day, agent, requests, input_tokens, output_tokens, cost_usd, updated_at)
            VALUES (?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(day, agent) DO UPDATE SET
                requests = requests + 1,
                input_tokens = input_tokens + excluded.input_tokens,
                output_tokens = output_tokens + excluded.output_tokens,
                cost_usd = cost_usd + excluded.cost_usd,
                updated_at = excluded.updated_at
            """,
            (bucket, agent, int(input_tokens), int(output_tokens), float(cost_usd), time.time()),
        )

    def daily_cost(self, day: str = "") -> dict:
        """Swarm-wide cost totals for a day (default: today), summed over agents."""
        bucket = day or time.strftime("%Y-%m-%d")
        row = self._db.read_one(
            """
            SELECT COALESCE(SUM(cost_usd), 0) AS cost_usd,
                   COALESCE(SUM(requests), 0) AS requests,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens
            FROM swarm_costs WHERE day = ?
            """,
            (bucket,),
        )
        return {
            "date": bucket,
            "cost_usd": round(float(row["cost_usd"]), 6) if row else 0.0,
            "requests": int(row["requests"]) if row else 0,
            "input_tokens": int(row["input_tokens"]) if row else 0,
            "output_tokens": int(row["output_tokens"]) if row else 0,
        }

    def per_agent_daily_cost(self, day: str = "") -> dict[str, float]:
        """Per-agent cost for a day → {agent: cost_usd}. For status/debug."""
        bucket = day or time.strftime("%Y-%m-%d")
        rows = self._db.read(
            "SELECT agent, cost_usd FROM swarm_costs WHERE day = ? ORDER BY cost_usd DESC",
            (bucket,),
        )
        return {r["agent"]: round(float(r["cost_usd"]), 6) for r in rows}

    # ------------------------------------------------------------------
    # Shared user facts — cross-agent view of the user
    # ------------------------------------------------------------------

    def add_shared_fact(
        self,
        *,
        user_id: str,
        fact: str,
        source_agent: str,
    ) -> bool:
        """Insert a user-derived fact. Returns True if new, False if duplicate."""
        fact = fact.strip()
        if not fact:
            return False
        now = time.time()
        cursor = self._db.write(
            """
            INSERT OR IGNORE INTO shared_user_facts
                (user_id, fact, source_agent, created_at, last_accessed_at, access_count)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (user_id, fact, source_agent, now, now),
        )
        return bool(cursor and cursor.rowcount)

    def search_shared_facts(
        self,
        *,
        user_id: str,
        query: str,
        limit: int = 5,
    ) -> list[str]:
        """FTS5 keyword search over shared facts for a user.

        Falls back to a plain LIKE match if FTS5 is unavailable or the
        query contains FTS5 special characters that we cannot safely
        escape for the MATCH operator.
        """
        query = query.strip()
        if not query:
            return []
        safe_query = " ".join(
            f'"{tok}"' for tok in query.split() if tok.strip()
        )
        if not safe_query:
            return []
        try:
            rows = self._db.read(
                """
                SELECT f.id, f.fact
                FROM shared_user_facts_fts fts
                JOIN shared_user_facts f ON f.id = fts.rowid
                WHERE fts.fact MATCH ?
                  AND f.user_id = ?
                ORDER BY rank
                LIMIT ?
                """,
                (safe_query, user_id, limit),
            )
        except Exception as e:
            log.warning("Shared facts FTS5 search failed, falling back: %s", e)
            like = f"%{query}%"
            rows = self._db.read(
                """
                SELECT id, fact FROM shared_user_facts
                WHERE user_id = ? AND fact LIKE ?
                ORDER BY last_accessed_at DESC
                LIMIT ?
                """,
                (user_id, like, limit),
            )

        if not rows:
            return []
        ids = tuple(int(r["id"]) for r in rows)
        # Touch accessed facts (read + recency bump) in one transaction.
        placeholders = ",".join("?" * len(ids))
        self._db.write(
            f"""
            UPDATE shared_user_facts
            SET access_count = access_count + 1,
                last_accessed_at = ?
            WHERE id IN ({placeholders})
            """,
            (time.time(), *ids),
        )
        return [r["fact"] for r in rows]

    def all_shared_facts(self, *, user_id: str, limit: int = 100) -> list[str]:
        rows = self._db.read(
            """
            SELECT fact FROM shared_user_facts
            WHERE user_id = ?
            ORDER BY last_accessed_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        return [r["fact"] for r in rows]

    # ------------------------------------------------------------------
    # Session messages — cross-agent FTS5 search
    # ------------------------------------------------------------------

    def index_session_message(
        self,
        *,
        agent_name: str,
        thread_id: str,
        role: str,
        content: str,
        fingerprint: str = "",
    ) -> bool:
        """Index a single message from a session into the cross-agent FTS store.

        ``fingerprint`` is optional for callers that do not have a stable
        per-session position. When absent, we derive a content-based key to
        keep repeated backfills from duplicating rows.
        """
        clean_content = content.strip()
        if not clean_content:
            return False
        fingerprint = fingerprint or hashlib.sha256(
            f"{agent_name}\0{thread_id}\0{role}\0{clean_content}".encode()
        ).hexdigest()
        cursor = self._db.write(
            """
            INSERT OR IGNORE INTO session_messages
                (agent_name, thread_id, role, content, created_at, fingerprint)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (agent_name, thread_id, role, clean_content, time.time(), fingerprint),
        )
        return bool(cursor and cursor.rowcount)

    def search_sessions(
        self,
        *,
        query: str,
        agent_name: str = "",
        days: int = 30,
        limit: int = 10,
    ) -> list[dict]:
        """FTS5 search over all session messages.

        Returns list of dicts with keys:
        agent_name, thread_id, role, content, created_at.
        """
        query = query.strip()
        if not query:
            return []
        safe_query = " ".join(f'"{tok}"' for tok in query.split() if tok.strip())
        if not safe_query:
            return []

        cutoff = time.time() - (days * 86400)

        try:
            if agent_name:
                rows = self._db.read(
                    """
                    SELECT sm.agent_name, sm.thread_id, sm.role,
                           sm.content, sm.created_at
                    FROM session_messages_fts fts
                    JOIN session_messages sm ON sm.id = fts.rowid
                    WHERE fts.content MATCH ?
                      AND sm.agent_name = ?
                      AND sm.created_at > ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (safe_query, agent_name, cutoff, limit),
                )
            else:
                rows = self._db.read(
                    """
                    SELECT sm.agent_name, sm.thread_id, sm.role,
                           sm.content, sm.created_at
                    FROM session_messages_fts fts
                    JOIN session_messages sm ON sm.id = fts.rowid
                    WHERE fts.content MATCH ?
                      AND sm.created_at > ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (safe_query, cutoff, limit),
                )
        except Exception as e:
            log.warning("Session FTS search failed, falling back to LIKE: %s", e)
            like = f"%{query}%"
            agent_filter = ""
            params: list = [like, cutoff, limit]
            if agent_name:
                agent_filter = "AND agent_name = ?"
                params = [like, agent_name, cutoff, limit]
            rows = self._db.read(
                f"""
                SELECT agent_name, thread_id, role, content, created_at
                FROM session_messages
                WHERE content LIKE ? {agent_filter}
                  AND created_at > ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                tuple(params),
            )

        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Feedback — Telegram reactions as RL signal
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_emoji(emoji: str) -> str:
        """Classify emoji into sentiment."""
        if emoji in POSITIVE_EMOJI:
            return "positive"
        if emoji in NEGATIVE_EMOJI:
            return "negative"
        return "neutral"

    def add_feedback(
        self,
        *,
        agent_name: str,
        chat_id: int,
        msg_id: int,
        emoji: str,
    ) -> bool:
        """Record a reaction as feedback. Returns True if new."""
        reaction = self._classify_emoji(emoji)
        cursor = self._db.write(
            """
            INSERT OR REPLACE INTO feedback
                (agent_name, chat_id, msg_id, reaction, emoji, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (agent_name, chat_id, msg_id, reaction, emoji, time.time()),
        )
        return bool(cursor and cursor.rowcount)

    def get_feedback(
        self,
        *,
        agent_name: str = "",
        reaction: str = "",
        days: int = 30,
        limit: int = 50,
    ) -> list[dict]:
        """Get feedback records with optional filters."""
        cutoff = time.time() - (days * 86400)
        conditions = ["created_at > ?"]
        params: list = [cutoff]

        if agent_name:
            conditions.append("agent_name = ?")
            params.append(agent_name)
        if reaction:
            conditions.append("reaction = ?")
            params.append(reaction)

        where = " AND ".join(conditions)
        params.append(limit)

        rows = self._db.read(
            f"""
            SELECT agent_name, chat_id, msg_id, reaction, emoji, created_at
            FROM feedback
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            tuple(params),
        )
        return [dict(r) for r in rows]

    def get_satisfaction_rate(
        self,
        *,
        agent_name: str = "",
        days: int = 7,
    ) -> dict:
        """Calculate satisfaction metrics."""
        cutoff = time.time() - (days * 86400)
        agent_filter = "AND agent_name = ?" if agent_name else ""
        params = (cutoff, agent_name) if agent_name else (cutoff,)

        rows = self._db.read(
            f"""
            SELECT reaction, COUNT(*) as cnt
            FROM feedback
            WHERE created_at > ? {agent_filter}
            GROUP BY reaction
            """,
            params,
        )

        counts = {r["reaction"]: int(r["cnt"]) for r in rows}
        total = sum(counts.values())
        positive = counts.get("positive", 0)
        negative = counts.get("negative", 0)

        rate = (positive / total * 100) if total > 0 else 0.0

        return {
            "total": total,
            "positive": positive,
            "negative": negative,
            "neutral": counts.get("neutral", 0),
            "satisfaction_rate": round(rate, 1),
            "days": days,
        }

    # ------------------------------------------------------------------
    # Retention (called by a periodic job — not wired in this step)
    # ------------------------------------------------------------------

    def prune_old_messages(self, older_than_days: int = MESSAGE_RETENTION_DAYS) -> int:
        cutoff = time.time() - older_than_days * 86400
        cursor = self._db.write(
            "DELETE FROM swarm_messages WHERE created_at < ?", (cutoff,),
        )
        deleted = cursor.rowcount if cursor is not None else 0
        log.info("Pruned %d swarm_messages older than %d days", deleted, older_than_days)
        return deleted

    def clear_thread_messages(self, *, chat_id: int, topic_id: int | None) -> int:
        """Delete the shared message ledger for one chat/topic (a /clear).

        So a cleared conversation is also gone from the cross-agent record.
        Does NOT touch shared_user_facts — learned facts are cross-conversation
        and are cleared by a separate 'forget' action, not by /clear.
        """
        cursor = self._db.write(
            "DELETE FROM swarm_messages WHERE chat_id = ? AND topic_id = ?",
            (chat_id, topic_id or 0),
        )
        deleted = cursor.rowcount if cursor is not None else 0
        log.info("Cleared %d swarm_messages for chat=%s topic=%s", deleted, chat_id, topic_id or 0)
        return deleted


_singleton: SwarmStore | None = None


def get_swarm() -> SwarmStore:
    """Process-wide singleton so schema init and lock are shared."""
    global _singleton
    if _singleton is None:
        _singleton = SwarmStore()
    return _singleton
