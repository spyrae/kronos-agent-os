"""Record which user a durable turn belongs to."""

import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Legacy turns stay ownerless: an empty owner is never matched to a requester."""
    await db.execute("BEGIN IMMEDIATE")
    try:
        async with db.execute("PRAGMA table_info(active_turns)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if "user_id" not in columns:
            await db.execute("ALTER TABLE active_turns ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
