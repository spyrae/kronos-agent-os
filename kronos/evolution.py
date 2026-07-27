"""Persona-evolution proposals (roadmap 6.3).

Weekly self-improvement proposes a concrete edit to SOUL/IDENTITY based on
feedback; the user approves or rejects in Telegram, and approval appends the
change to the target persona file with provenance. Proposals are per-agent.
"""

import time
from datetime import UTC, datetime

from kronos.db import get_db

# Which persona files a proposal may target.
VALID_TARGETS = ("soul", "identity")


def _init_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS persona_proposals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name  TEXT    NOT NULL,
            target      TEXT    NOT NULL,
            rationale   TEXT    NOT NULL,
            proposal    TEXT    NOT NULL,
            state       TEXT    NOT NULL DEFAULT 'pending',
            created_at  REAL    NOT NULL,
            decided_at  REAL
        );
        CREATE INDEX IF NOT EXISTS idx_persona_proposals_pending
            ON persona_proposals(agent_name, state, created_at);
        """
    )

    # Measurement of the proposal (moat 12.4). Added by migration so an existing
    # database keeps its pending proposals.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(persona_proposals)").fetchall()}
    if "eval_json" not in columns:
        conn.execute("ALTER TABLE persona_proposals ADD COLUMN eval_json TEXT")


def _db():
    db = get_db("persona_proposals")
    db.init_schema(_init_schema)
    return db


def create_proposal(
    *,
    agent_name: str,
    target: str,
    rationale: str,
    proposal: str,
    eval_json: str = "",
) -> int:
    """Insert a pending proposal and return its id."""
    cursor = _db().write(
        "INSERT INTO persona_proposals "
        "(agent_name, target, rationale, proposal, state, created_at, eval_json) "
        "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
        (agent_name, target, rationale, proposal, time.time(), eval_json),
    )
    return int(cursor.lastrowid)


def list_proposals(agent_name: str, *, state: str = "", limit: int = 20) -> list[dict]:
    """Recent proposals, optionally filtered by state (including rejected ones).

    Auto-rejected proposals are listed here on purpose: a measurement that hides
    what it rejected is indistinguishable from one that never ran.
    """
    if state:
        rows = _db().read(
            "SELECT * FROM persona_proposals WHERE agent_name=? AND state=? ORDER BY created_at DESC LIMIT ?",
            (agent_name, state, limit),
        )
    else:
        rows = _db().read(
            "SELECT * FROM persona_proposals WHERE agent_name=? ORDER BY created_at DESC LIMIT ?",
            (agent_name, limit),
        )
    return [dict(row) for row in rows]


def record_decision_reason(proposal_id: int, reason: str) -> None:
    """Attach why a proposal was decided, inside the stored measurement."""
    import json as _json

    row = _db().read_one("SELECT eval_json FROM persona_proposals WHERE id=?", (proposal_id,))
    try:
        payload = _json.loads(row["eval_json"]) if row and row["eval_json"] else {}
    except (TypeError, ValueError):
        payload = {}
    payload["decision_reason"] = reason
    _db().write("UPDATE persona_proposals SET eval_json=? WHERE id=?", (_json.dumps(payload), proposal_id))


def list_pending(agent_name: str) -> list[dict]:
    rows = _db().read(
        "SELECT * FROM persona_proposals WHERE agent_name=? AND state='pending' ORDER BY created_at",
        (agent_name,),
    )
    return [dict(row) for row in rows]


def get_proposal(proposal_id: int, agent_name: str) -> dict | None:
    row = _db().read_one(
        "SELECT * FROM persona_proposals WHERE id=? AND agent_name=?",
        (proposal_id, agent_name),
    )
    return dict(row) if row else None


def decide_proposal(proposal_id: int, agent_name: str, *, approved: bool) -> dict | None:
    """Atomically move a pending proposal to approved/rejected. Returns it or None.

    IMMEDIATE transaction so a proposal is decided exactly once.
    """

    def _tx(conn):
        row = conn.execute(
            "SELECT * FROM persona_proposals WHERE id=? AND agent_name=? AND state='pending'",
            (proposal_id, agent_name),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE persona_proposals SET state=?, decided_at=? WHERE id=?",
            ("approved" if approved else "rejected", time.time(), proposal_id),
        )
        return dict(row)

    return _db().write_tx(_tx)


def apply_proposal(proposal: dict) -> str:
    """Append an approved proposal to its target persona file. Returns the path.

    Append-only (never rewrites) so an accepted change can't corrupt the file;
    each edit is stamped with its rationale for provenance.
    """
    import kronos.workspace as _workspace

    ws = _workspace.ws
    targets = {"soul": ws.soul, "identity": ws.identity}
    target_file = targets[proposal["target"]]
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    section = f"\n\n## Evolution {stamp} (approved)\n_{proposal['rationale']}_\n\n{proposal['proposal']}\n"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    with open(target_file, "a", encoding="utf-8") as handle:
        handle.write(section)
    return str(target_file)
