"""Keep the operator's verdict on an effect whose outcome the runtime never learned."""

import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Additive audit table; unresolved intents themselves are left untouched."""
    await db.execute("BEGIN IMMEDIATE")
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS effect_reconciliations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                tool TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                args_json TEXT NOT NULL,
                args_fingerprint TEXT NOT NULL,
                intent_created_at TEXT,
                outcome TEXT NOT NULL CHECK(outcome IN ('happened', 'did_not_happen', 'unknown')),
                result TEXT NOT NULL DEFAULT '',
                operator TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                reconciled_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_effect_reconciliations_turn ON effect_reconciliations(turn_id)"
        )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
