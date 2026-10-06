"""Remember which conversation a durable turn is charged to."""

import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Legacy turns keep an empty value and fall back to their thread id."""
    await db.execute("BEGIN IMMEDIATE")
    try:
        async with db.execute("PRAGMA table_info(active_turns)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if "budget_session" not in columns:
            await db.execute("ALTER TABLE active_turns ADD COLUMN budget_session TEXT NOT NULL DEFAULT ''")
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
