"""Reserve external effects before invoking tools, not after they return."""

import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Create the additive intent ledger; retain existing recorded effects."""
    await db.execute("BEGIN IMMEDIATE")
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS effect_intents (
                idempotency_key TEXT PRIMARY KEY,
                turn_id TEXT NOT NULL,
                tool TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                dedupe_by_key INTEGER NOT NULL DEFAULT 0,
                args_json TEXT NOT NULL,
                args_fingerprint TEXT NOT NULL,
                claim_token TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'recorded')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_effect_intents_turn ON effect_intents(turn_id, status)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_effect_intents_operation ON effect_intents(tool, args_fingerprint, status)"
        )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
