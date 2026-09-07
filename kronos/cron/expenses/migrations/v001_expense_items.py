"""Persist extraction snapshots and per-item progress (review F05)."""


def migrate(conn) -> None:
    """Apply the idempotent first item-ledger migration atomically."""
    try:
        conn.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS email_expense_items (
                message_id TEXT NOT NULL,
                item_index INTEGER NOT NULL,
                source TEXT NOT NULL,
                expense_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ready'
                    CHECK(status IN ('ready', 'writing', 'recorded', 'duplicate',
                                     'pending', 'discarded', 'error', 'uncertain')),
                amount_idr REAL,
                expense_date TEXT,
                pending_id INTEGER UNIQUE,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (message_id, item_index)
            );
            CREATE INDEX IF NOT EXISTS idx_email_items_dedup
                ON email_expense_items(expense_date, amount_idr, status);
            INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_expense_items', '1');
            COMMIT;
        """)
    except BaseException:
        conn.rollback()
        raise
