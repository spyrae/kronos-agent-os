"""FTS5 keyword search index for memories.

Runs in parallel with Qdrant vector search. Stores the same facts
that Mem0 extracts, indexed for exact keyword matching (names,
dates, numbers, IDs — things vector search misses).

Schema mirrors Kronos I recall.py but adapted for Mem0 integration.

All database access goes through SafeDB for thread-safe write serialization.
"""

import logging
import re
import sqlite3
from datetime import datetime, timezone

from kronos.db import get_db

log = logging.getLogger("kronos.memory.fts")

_schema_initialized = False
_schema_lock = __import__("threading").Lock()


def _get_db():
    """Get the FTS database with lazy schema init (thread-safe)."""
    global _schema_initialized
    if not _schema_initialized:
        with _schema_lock:
            if not _schema_initialized:
                db = get_db("memory_fts")
                db.init_schema(_init_schema)
                _schema_initialized = True
    return get_db("memory_fts")


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memory_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT DEFAULT 'mem0',
            created_at TEXT NOT NULL,
            mem0_id TEXT
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
            content,
            user_id UNINDEXED,
            tokenize='unicode61'
        );

        CREATE INDEX IF NOT EXISTS idx_facts_user ON memory_facts(user_id);
    """)

    # Ebbinghaus decay columns (added incrementally)
    _migrate_add_decay_columns(conn)
    conn.commit()


def _migrate_add_decay_columns(conn: sqlite3.Connection) -> None:
    """Add relevance/tier/last_accessed columns if not present."""
    cursor = conn.execute("PRAGMA table_info(memory_facts)")
    columns = {row[1] for row in cursor.fetchall()}

    if "relevance" not in columns:
        conn.execute("ALTER TABLE memory_facts ADD COLUMN relevance REAL DEFAULT 1.0")
        log.info("Migration: added 'relevance' column to memory_facts")

    if "tier" not in columns:
        conn.execute("ALTER TABLE memory_facts ADD COLUMN tier TEXT DEFAULT 'active'")
        log.info("Migration: added 'tier' column to memory_facts")

    if "last_accessed" not in columns:
        conn.execute("ALTER TABLE memory_facts ADD COLUMN last_accessed TEXT")
        conn.execute("UPDATE memory_facts SET last_accessed = created_at WHERE last_accessed IS NULL")
        log.info("Migration: added 'last_accessed' column to memory_facts")


def index_fact(content: str, user_id: str, mem0_id: str = "") -> None:
    """Index a single memory fact into FTS5."""
    if not content or len(content.strip()) < 3:
        return

    db = _get_db()
    now = datetime.now(timezone.utc).isoformat()

    def _do_index(conn):
        existing = conn.execute(
            "SELECT id FROM memory_facts WHERE user_id = ? AND content = ?",
            (user_id, content),
        ).fetchone()
        if existing:
            return

        cursor = conn.execute(
            "INSERT INTO memory_facts (user_id, content, created_at, mem0_id) VALUES (?, ?, ?, ?)",
            (user_id, content, now, mem0_id),
        )
        row_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO memory_fts (rowid, content, user_id) VALUES (?, ?, ?)",
            (row_id, content, user_id),
        )

    db.write_tx(_do_index)


def index_facts_batch(facts: list[str], user_id: str) -> int:
    """Index multiple facts at once. Returns count of new facts indexed."""
    count = 0
    for fact in facts:
        fact = fact.strip()
        if fact:
            try:
                index_fact(fact, user_id)
                count += 1
            except Exception as e:
                log.warning("Failed to index fact: %s", e)
    return count


def search(query: str, user_id: str, limit: int = 10) -> list[dict]:
    """Search FTS5 index for keyword matches.

    Returns list of dicts with 'content', 'rank', 'created_at'.
    Rank is negative (FTS5 convention), lower = better match.
    """
    db = _get_db()

    # Sanitize query for FTS5
    safe_query = _sanitize_fts_query(query)
    if not safe_query:
        return []

    try:
        rows = db.read(
            """
            SELECT mf.content, mf.created_at, memory_fts.rank,
                   COALESCE(mf.relevance, 1.0) as relevance,
                   COALESCE(mf.tier, 'active') as tier
            FROM memory_fts
            JOIN memory_facts mf ON mf.id = memory_fts.rowid
            WHERE memory_fts MATCH ?
              AND mf.user_id = ?
              AND COALESCE(mf.tier, 'active') != 'archive'
            ORDER BY (memory_fts.rank * (1.0 / COALESCE(mf.relevance, 1.0)))
            LIMIT ?
            """,
            (safe_query, user_id, limit),
        )

        return [
            {
                "content": row["content"],
                "created_at": row["created_at"],
                "rank": row["rank"],
                "relevance": row["relevance"],
                "tier": row["tier"],
            }
            for row in rows
        ]

    except sqlite3.OperationalError as e:
        log.debug("FTS5 search failed for '%s': %s", safe_query, e)
        return []


def _sanitize_fts_query(query: str) -> str:
    """Sanitize query for FTS5 MATCH syntax.

    Strips special chars, splits into tokens, joins with implicit AND.
    """
    # Remove FTS5 operators and special chars
    cleaned = re.sub(r'[^\w\s\-]', ' ', query)
    tokens = cleaned.split()
    # Filter short tokens (noise) but keep numbers/IDs
    tokens = [t for t in tokens if len(t) >= 2 or t.isdigit()]
    if not tokens:
        return ""
    # Quote each token to prevent FTS5 syntax errors
    return " ".join(f'"{t}"' for t in tokens[:10])  # cap at 10 tokens


def touch_facts(fact_contents: list[str], user_id: str) -> int:
    """Mark facts as accessed (updates last_accessed + boosts relevance).

    Called when facts are returned from search — keeps accessed facts alive.
    Non-critical: failures are logged but don't propagate.
    """
    if not fact_contents:
        return 0

    try:
        db = _get_db()
        now = datetime.now(timezone.utc).isoformat()

        ops = []
        for content in fact_contents:
            ops.append((
                """UPDATE memory_facts
                   SET last_accessed = ?,
                       relevance = MIN(1.0, relevance + 0.05)
                   WHERE user_id = ? AND content = ?""",
                (now, user_id, content),
            ))

        if ops:
            db.write_many(ops)
        return len(fact_contents)
    except Exception as e:
        log.warning("touch_facts failed (non-critical): %s", e)
        return 0


def decay_all_facts(half_life_days: int = 14) -> dict:
    """Apply Ebbinghaus forgetting curve to all facts.

    Relevance decays based on days since last access:
      new_relevance = relevance * 2^(-days_since_access / half_life)

    Tiers based on relevance:
      active (>= 0.6) → warm (0.3-0.6) → cold (0.1-0.3) → archive (< 0.1)

    Returns stats dict.
    """
    import math

    db = _get_db()
    now = datetime.now(timezone.utc)

    rows = db.read(
        "SELECT id, relevance, last_accessed, tier FROM memory_facts WHERE relevance > 0.01"
    )

    stats = {"decayed": 0, "tier_changes": 0, "archived": 0}
    ops = []

    for row in rows:
        last_acc = row["last_accessed"] or row.get("created_at", now.isoformat())
        try:
            last_dt = datetime.fromisoformat(last_acc.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        days_since = max(0, (now - last_dt).total_seconds() / 86400)
        old_relevance = row["relevance"] or 1.0

        new_relevance = old_relevance * math.pow(2, -days_since / half_life_days)
        new_relevance = round(max(0.01, new_relevance), 4)

        if new_relevance >= 0.6:
            new_tier = "active"
        elif new_relevance >= 0.3:
            new_tier = "warm"
        elif new_relevance >= 0.1:
            new_tier = "cold"
        else:
            new_tier = "archive"

        if abs(new_relevance - old_relevance) > 0.001 or new_tier != row["tier"]:
            ops.append((
                "UPDATE memory_facts SET relevance = ?, tier = ? WHERE id = ?",
                (new_relevance, new_tier, row["id"]),
            ))
            stats["decayed"] += 1
            if new_tier != row["tier"]:
                stats["tier_changes"] += 1
            if new_tier == "archive":
                stats["archived"] += 1

    if ops:
        db.write_many(ops)

    log.info(
        "Decay complete: %d facts decayed, %d tier changes, %d archived",
        stats["decayed"], stats["tier_changes"], stats["archived"],
    )
    return stats


def get_tier_distribution() -> dict[str, int]:
    """Get count of facts per tier."""
    db = _get_db()
    rows = db.read(
        "SELECT COALESCE(tier, 'active') as tier, COUNT(*) as cnt FROM memory_facts GROUP BY tier"
    )
    return {row["tier"]: row["cnt"] for row in rows}


def get_stats(user_id: str | None = None) -> dict:
    """Get FTS index statistics."""
    db = _get_db()
    if user_id:
        row = db.read_one(
            "SELECT COUNT(*) as total FROM memory_facts WHERE user_id = ?", (user_id,)
        )
    else:
        row = db.read_one("SELECT COUNT(*) as total FROM memory_facts")

    total = row["total"] if row else 0
    from kronos.db import get_db as _gdb
    db_path = _gdb("memory_fts")._db_path
    db_size = db_path.stat().st_size if db_path.exists() else 0
    return {"total_facts": total, "db_size_kb": db_size // 1024}
