"""Track whether an ordinary reply reached the transport."""

import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Existing turns owe nothing: their replies were sent or lost before this was tracked."""
    await db.execute("BEGIN IMMEDIATE")
    try:
        async with db.execute("PRAGMA table_info(active_turns)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if "reply_state" not in columns:
            await db.execute("ALTER TABLE active_turns ADD COLUMN reply_state TEXT NOT NULL DEFAULT ''")
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
