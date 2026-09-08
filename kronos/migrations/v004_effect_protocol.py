"""Distinguish intent-protected turns from pre-protocol legacy executions."""

import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Legacy rows stay zero: a missing intent there is not proof of no dispatch."""
    await db.execute("BEGIN IMMEDIATE")
    try:
        async with db.execute("PRAGMA table_info(active_turns)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if "effect_protocol" not in columns:
            await db.execute("ALTER TABLE active_turns ADD COLUMN effect_protocol INTEGER NOT NULL DEFAULT 0")
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
