"""Retain the exact terminal response after deleting a turn's journal."""

import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Add nullable final_content atomically, including concurrent startups.

    NULL means no recorded outcome (legacy rows), not an empty successful
    response. The caller must finish any preceding transaction first.
    """
    await db.execute("BEGIN IMMEDIATE")
    try:
        async with db.execute("PRAGMA table_info(active_turns)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if "final_content" not in columns:
            await db.execute("ALTER TABLE active_turns ADD COLUMN final_content TEXT")
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
