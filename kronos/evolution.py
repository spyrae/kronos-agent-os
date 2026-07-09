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


def _db():
    db = get_db("persona_proposals")
    db.init_schema(_init_schema)
    return db


def create_proposal(*, agent_name: str, target: str, rationale: str, proposal: str) -> int:
    """Insert a pending proposal and return its id."""
    cursor = _db().write(
        "INSERT INTO persona_proposals "
        "(agent_name, target, rationale, proposal, state, created_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?)",
        (agent_name, target, rationale, proposal, time.time()),
    )
    return int(cursor.lastrowid)


def list_pending(agent_name: str) -> list[dict]:
    rows = _db().read(
        "SELECT * FROM persona_proposals "
        "WHERE agent_name=? AND state='pending' ORDER BY created_at",
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
            "SELECT * FROM persona_proposals "
            "WHERE id=? AND agent_name=? AND state='pending'",
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
    section = (
        f"\n\n## Evolution {stamp} (approved)\n"
        f"_{proposal['rationale']}_\n\n"
        f"{proposal['proposal']}\n"
    )
    target_file.parent.mkdir(parents=True, exist_ok=True)
    with open(target_file, "a", encoding="utf-8") as handle:
        handle.write(section)
    return str(target_file)
