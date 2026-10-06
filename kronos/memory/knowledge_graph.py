"""L3 Semantic Memory — Knowledge Graph in SQLite.

Stores entities (people, companies, projects, concepts) and
relationships between them. Built incrementally from conversations.

Schema:
  entities: id, user_id, name, type, properties (JSON), created_at, updated_at
  relations: id, user_id, source_id, target_id, relation_type, properties, created_at

Entity types: person, company, project, concept, tool, location, event
Relation types: knows, works_at, uses, owns, related_to, part_of, created

Every row belongs to one user. Writes require the owner, reads return only the
owner's rows, and an empty owner matches nothing. Rows created before owner
scoping keep ``user_id IS NULL``: nobody reads them until an operator assigns
them (see ``assign_legacy_rows``).

All database access goes through SafeDB for thread-safe write serialization.
"""

import json
import logging
import sqlite3
from datetime import UTC, datetime

from kronos.db import get_db

log = logging.getLogger("kronos.memory.knowledge_graph")

_schema_initialized = False
_schema_lock = __import__("threading").Lock()


def _get_db():
    """Get the KG database with lazy schema init (thread-safe)."""
    global _schema_initialized
    if not _schema_initialized:
        with _schema_lock:
            if not _schema_initialized:
                db = get_db("knowledge_graph")
                db.init_schema(_init_schema)
                _schema_initialized = True
    return get_db("knowledge_graph")


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            properties TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            user_id TEXT
        );

        CREATE TABLE IF NOT EXISTS relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES entities(id),
            target_id INTEGER NOT NULL REFERENCES entities(id),
            relation_type TEXT NOT NULL,
            properties TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            user_id TEXT
        );
    """)
    _migrate_add_owner(conn)
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_entity_name ON entities(name);
        CREATE INDEX IF NOT EXISTS idx_entity_type ON entities(type);
        CREATE INDEX IF NOT EXISTS idx_entity_owner ON entities(user_id, updated_at);
        CREATE INDEX IF NOT EXISTS idx_relation_source ON relations(source_id);
        CREATE INDEX IF NOT EXISTS idx_relation_target ON relations(target_id);
        CREATE INDEX IF NOT EXISTS idx_relation_owner ON relations(user_id);
        -- The same name may exist once per owner; ownerless legacy rows count as one owner.
        DROP INDEX IF EXISTS idx_entity_unique;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_owner_unique
            ON entities(COALESCE(user_id, ''), name, type);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_relation_unique
            ON relations(source_id, target_id, relation_type);
    """)


def _migrate_add_owner(conn: sqlite3.Connection) -> None:
    """Add the owner column. Existing rows stay NULL: their owner was never recorded."""
    for table in ("entities", "relations"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "user_id" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT")
            log.info("Migration: added 'user_id' column to %s", table)


def _write_owner(user_id: str) -> str:
    owner = str(user_id or "").strip()
    if not owner:
        raise ValueError("knowledge graph writes require the owning user_id")
    return owner


def _read_owner(user_id: str) -> str:
    return str(user_id or "").strip()


# --- Entity operations ---


def add_entity(name: str, entity_type: str, properties: dict | None = None, *, user_id: str) -> int:
    """Add or update one of the owner's entities. Returns entity ID."""
    owner = _write_owner(user_id)
    db = _get_db()
    now = datetime.now(UTC).isoformat()
    props = json.dumps(properties or {}, ensure_ascii=False)

    def _upsert(conn):
        existing = conn.execute(
            "SELECT id FROM entities WHERE user_id = ? AND name = ? AND type = ?",
            (owner, name, entity_type),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE entities SET properties = ?, updated_at = ? WHERE id = ?",
                (props, now, existing["id"]),
            )
            return existing["id"]

        cursor = conn.execute(
            "INSERT INTO entities (user_id, name, type, properties, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (owner, name, entity_type, props, now, now),
        )
        return cursor.lastrowid

    return db.write_tx(_upsert)


def get_entity(name: str, entity_type: str | None = None, *, user_id: str) -> dict | None:
    """Find one of the owner's entities by name (and optionally type)."""
    owner = _read_owner(user_id)
    if not owner:
        return None
    db = _get_db()
    if entity_type:
        row = db.read_one(
            "SELECT * FROM entities WHERE user_id = ? AND name = ? AND type = ?",
            (owner, name, entity_type),
        )
    else:
        row = db.read_one(
            "SELECT * FROM entities WHERE user_id = ? AND name = ? ORDER BY updated_at DESC",
            (owner, name),
        )

    if not row:
        return None

    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "properties": json.loads(row["properties"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def search_entities(query: str, entity_type: str | None = None, limit: int = 10, *, user_id: str) -> list[dict]:
    """Search the owner's entities by name (LIKE match)."""
    owner = _read_owner(user_id)
    if not owner:
        return []
    db = _get_db()
    if entity_type:
        rows = db.read(
            "SELECT * FROM entities WHERE user_id = ? AND name LIKE ? AND type = ? ORDER BY updated_at DESC LIMIT ?",
            (owner, f"%{query}%", entity_type, limit),
        )
    else:
        rows = db.read(
            "SELECT * FROM entities WHERE user_id = ? AND name LIKE ? ORDER BY updated_at DESC LIMIT ?",
            (owner, f"%{query}%", limit),
        )

    return [
        {
            "id": r["id"],
            "name": r["name"],
            "type": r["type"],
            "properties": json.loads(r["properties"]),
        }
        for r in rows
    ]


# --- Relation operations ---


def add_relation(
    source_name: str,
    source_type: str,
    target_name: str,
    target_type: str,
    relation_type: str,
    properties: dict | None = None,
    *,
    user_id: str,
) -> int:
    """Add a relation between two of the owner's entities (creates them if needed)."""
    owner = _write_owner(user_id)
    source_id = add_entity(source_name, source_type, user_id=owner)
    target_id = add_entity(target_name, target_type, user_id=owner)

    db = _get_db()
    now = datetime.now(UTC).isoformat()
    props = json.dumps(properties or {}, ensure_ascii=False)

    def _upsert(conn):
        existing = conn.execute(
            "SELECT id FROM relations WHERE source_id = ? AND target_id = ? AND relation_type = ?",
            (source_id, target_id, relation_type),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE relations SET properties = ? WHERE id = ?",
                (props, existing["id"]),
            )
            return existing["id"]

        cursor = conn.execute(
            "INSERT INTO relations (user_id, source_id, target_id, relation_type, properties, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (owner, source_id, target_id, relation_type, props, now),
        )
        return cursor.lastrowid

    return db.write_tx(_upsert)


def get_connections(entity_name: str, depth: int = 1, *, user_id: str) -> list[dict]:
    """Get the owner's connections for one of their entities."""
    owner = _read_owner(user_id)
    entity = get_entity(entity_name, user_id=owner)
    if not entity:
        return []
    db = _get_db()

    eid = entity["id"]

    # Both the relation and the far entity must belong to the owner, so a
    # corrupt cross-owner link can never surface another user's entity.
    rows = db.read(
        """
        SELECT e2.name, e2.type, r.relation_type, 'outgoing' as direction
        FROM relations r
        JOIN entities e2 ON e2.id = r.target_id
        WHERE r.source_id = ? AND r.user_id = ? AND e2.user_id = ?
        UNION ALL
        SELECT e2.name, e2.type, r.relation_type, 'incoming' as direction
        FROM relations r
        JOIN entities e2 ON e2.id = r.source_id
        WHERE r.target_id = ? AND r.user_id = ? AND e2.user_id = ?
        """,
        (eid, owner, owner, eid, owner, owner),
    )

    return [
        {
            "entity": r["name"],
            "entity_type": r["type"],
            "relation": r["relation_type"],
            "direction": r["direction"],
        }
        for r in rows
    ]


def get_graph_context(query: str, limit: int = 5, *, user_id: str) -> str:
    """Get the owner's knowledge graph context relevant to a query.

    Searches entities matching the query and returns their connections
    as a formatted string for LLM context injection.
    """
    entities = search_entities(query, limit=limit, user_id=user_id)
    if not entities:
        return ""

    parts = []
    for entity in entities:
        connections = get_connections(entity["name"], user_id=user_id)
        if connections:
            conn_text = ", ".join(f"{c['relation']}→{c['entity']}({c['entity_type']})" for c in connections[:5])
            parts.append(f"{entity['name']} ({entity['type']}): {conn_text}")
        else:
            parts.append(f"{entity['name']} ({entity['type']})")

    return "\n".join(parts)


def recent_entities(limit: int = 20) -> list[dict]:
    """Operator view across owners: the most recently updated owned entities."""
    rows = _get_db().read(
        "SELECT name, type, user_id FROM entities WHERE user_id IS NOT NULL ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    )
    return [{"name": r["name"], "type": r["type"], "user_id": r["user_id"]} for r in rows]


def delete_rows(*, user_id: str = "") -> dict[str, int]:
    """Reset: delete one owner's graph, or every row (ownerless too) when ``user_id`` is empty."""
    owner = _read_owner(user_id)

    def _delete(conn):
        if owner:
            relations = conn.execute(
                """
                DELETE FROM relations WHERE user_id = ?
                   OR source_id IN (SELECT id FROM entities WHERE user_id = ?)
                   OR target_id IN (SELECT id FROM entities WHERE user_id = ?)
                """,
                (owner, owner, owner),
            ).rowcount
            entities = conn.execute("DELETE FROM entities WHERE user_id = ?", (owner,)).rowcount
        else:
            relations = conn.execute("DELETE FROM relations").rowcount
            entities = conn.execute("DELETE FROM entities").rowcount
        return {"entities": entities, "relations": relations}

    return _get_db().write_tx(_delete)


def legacy_row_counts() -> dict[str, int]:
    """How many rows still have no recorded owner."""
    db = _get_db()
    return {
        "entities": db.read_one("SELECT COUNT(*) AS cnt FROM entities WHERE user_id IS NULL")["cnt"],
        "relations": db.read_one("SELECT COUNT(*) AS cnt FROM relations WHERE user_id IS NULL")["cnt"],
    }


def assign_legacy_rows(user_id: str) -> dict[str, int]:
    """Operator decision: hand ownerless rows to ``user_id``.

    Never called by the runtime. An entity the owner already has under the same
    name and type stays ownerless rather than being merged or deleted, and a
    relation moves only when both of its entities now belong to the owner.
    """
    owner = _write_owner(user_id)

    def _assign(conn):
        entities = conn.execute(
            """
            UPDATE entities SET user_id = ?
            WHERE user_id IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM entities mine
                  WHERE mine.user_id = ? AND mine.name = entities.name AND mine.type = entities.type
              )
            """,
            (owner, owner),
        ).rowcount
        relations = conn.execute(
            """
            UPDATE relations SET user_id = ?
            WHERE user_id IS NULL
              AND (SELECT user_id FROM entities WHERE id = relations.source_id) = ?
              AND (SELECT user_id FROM entities WHERE id = relations.target_id) = ?
            """,
            (owner, owner, owner),
        ).rowcount
        return {"entities": entities, "relations": relations}

    assigned = _get_db().write_tx(_assign)
    log.info("Assigned %d legacy entities and %d relations to one owner", assigned["entities"], assigned["relations"])
    return assigned


def get_stats() -> dict:
    """Get knowledge graph statistics."""
    db = _get_db()
    entities = db.read_one("SELECT COUNT(*) as cnt FROM entities")["cnt"]
    relations = db.read_one("SELECT COUNT(*) as cnt FROM relations")["cnt"]
    types = db.read("SELECT type, COUNT(*) as cnt FROM entities GROUP BY type")
    return {
        "entities": entities,
        "relations": relations,
        "by_type": {r["type"]: r["cnt"] for r in types},
    }
