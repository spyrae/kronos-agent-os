"""Correlate plan steps with durable turns and approval notifications."""

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    """Upgrade legacy plan rows without inferring missing execution links."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(plan_steps)")}
        for column in ("turn_id", "approval_id", "notified_approval_id"):
            if column not in columns:
                conn.execute(f"ALTER TABLE plan_steps ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_plan_steps_turn ON plan_steps(turn_id)")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
