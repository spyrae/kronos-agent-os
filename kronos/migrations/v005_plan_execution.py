"""Join a plan claim to turn creation across their two SQLite databases."""

import sqlite3

import aiosqlite


def migrate_plans(conn: sqlite3.Connection) -> None:
    """Legacy claims stay uncorrelated; never infer that they did no work."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(plan_steps)")}
        for name in ("execution_key", "last_turn_id"):
            if name not in columns:
                conn.execute(f"ALTER TABLE plan_steps ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        if "repark_requested" not in columns:
            conn.execute("ALTER TABLE plan_steps ADD COLUMN repark_requested INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


async def migrate_turns(db: aiosqlite.Connection) -> None:
    """Keep an immutable unique caller key from the same commit as begin_turn."""
    await db.execute("BEGIN IMMEDIATE")
    try:
        async with db.execute("PRAGMA table_info(active_turns)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if "caller_key" not in columns:
            await db.execute("ALTER TABLE active_turns ADD COLUMN caller_key TEXT NOT NULL DEFAULT ''")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_active_turns_caller ON active_turns(caller_key) WHERE caller_key != ''"
        )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
