"""Delivery obligations live in the producer's database, not a second ledger."""

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    """Add the reusable outbox and opt-in for new cancellation notices."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS delivery_outbox (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            stream_key TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            topic_id INTEGER,
            chunks TEXT NOT NULL,
            random_ids TEXT NOT NULL,
            sender_id INTEGER NOT NULL DEFAULT 0,
            next_chunk INTEGER NOT NULL DEFAULT 0,
            receipts TEXT NOT NULL DEFAULT '[]',
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'delivered', 'needs_review')),
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_due ON delivery_outbox(state, next_attempt, seq)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_stream ON delivery_outbox(stream_key, seq, state)")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(plans)")}
        if columns and "stop_notify" not in columns:
            conn.execute("ALTER TABLE plans ADD COLUMN stop_notify INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
