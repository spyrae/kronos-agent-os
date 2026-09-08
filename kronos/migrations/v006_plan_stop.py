"""Track stop reconciliation separately from a plan's stop request."""

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    """Legacy cancelled/expired plans still require fenced cleanup."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(plan_steps)")}
        if "stop_reconciled" not in columns:
            conn.execute("ALTER TABLE plan_steps ADD COLUMN stop_reconciled INTEGER NOT NULL DEFAULT 0")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(plans)")}
        if "stop_reason" not in columns:
            conn.execute("ALTER TABLE plans ADD COLUMN stop_reason TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE plans SET stop_reason = 'plan_cancelled' WHERE state = 'cancelled'")
            conn.execute("""UPDATE plans SET stop_reason = 'plan_expired' WHERE state = 'failed'
                AND EXISTS (SELECT 1 FROM plan_steps s WHERE s.plan_id = plans.id
                    AND s.result LIKE 'plan expired%')""")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
