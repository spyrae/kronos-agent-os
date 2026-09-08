"""Persist recovery routing and let obsolete approval notices stay auditable."""

import sqlite3

import aiosqlite

from kronos.migrations.v007_delivery_outbox import OUTBOX_SCHEMA


def migrate_queue(conn: sqlite3.Connection) -> None:
    """Upgrade a plan queue without rewriting messages or their random IDs."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(delivery_outbox)")}
        if "obsolete" not in columns:
            conn.execute("ALTER TABLE delivery_outbox ADD COLUMN obsolete INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


async def migrate(db: aiosqlite.Connection) -> None:
    """Legacy routes stay unknown; never guess a Telegram destination."""
    await db.execute("BEGIN IMMEDIATE")
    try:
        for statement in OUTBOX_SCHEMA:
            await db.execute(statement)
        async with db.execute("PRAGMA table_info(delivery_outbox)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if "obsolete" not in columns:
            await db.execute("ALTER TABLE delivery_outbox ADD COLUMN obsolete INTEGER NOT NULL DEFAULT 0")
        async with db.execute("PRAGMA table_info(active_turns)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        for name, definition in (
            ("recovery_destination", "TEXT NOT NULL DEFAULT ''"),
            ("delivery_requested", "INTEGER NOT NULL DEFAULT 0"),
            ("delivery_issue", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in columns:
                await db.execute(f"ALTER TABLE active_turns ADD COLUMN {name} {definition}")
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
