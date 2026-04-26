"""L3 Semantic Memory — Knowledge Graph in SQLite.

Stores entities (people, companies, projects, concepts) and
relationships between them. Built incrementally from conversations.

Schema:
  entities: id, name, type, properties (JSON), created_at, updated_at
  relations: id, source_id, target_id, relation_type, properties, created_at

Entity types: person, company, project, concept, tool, location, event
Relation types: knows, works_at, uses, owns, related_to, part_of, created

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
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES entities(id),
            target_id INTEGER NOT NULL REFERENCES entities(id),
            relation_type TEXT NOT NULL,
            properties TEXT DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_entity_name ON entities(name);
        CREATE INDEX IF NOT EXISTS idx_entity_type ON entities(type);
        CREATE INDEX IF NOT EXISTS idx_relation_source ON relations(source_id);
        CREATE INDEX IF NOT EXISTS idx_relation_target ON relations(target_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_unique ON entities(name, type);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_relation_unique
            ON relations(source_id, target_id, relation_type);
    """)


# --- Entity operations ---

def add_entity(name: str, entity_type: str, properties: dict | None = None) -> int:
    """Add or update an entity. Returns entity ID."""
    db = _get_db()
    now = datetime.now(UTC).isoformat()
    props = json.dumps(properties or {}, ensure_ascii=False)

    def _upsert(conn):
        existing = conn.execute(
            "SELECT id FROM entities WHERE name = ? AND type = ?",
            (name, entity_type),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE entities SET properties = ?, updated_at = ? WHERE id = ?",
                (props, now, existing["id"]),
            )
            return existing["id"]

        cursor = conn.execute(
            "INSERT INTO entities (name, type, properties, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (name, entity_type, props, now, now),
        )
        return cursor.lastrowid

    return db.write_tx(_upsert)


def get_entity(name: str, entity_type: str | None = None) -> dict | None:
    """Find entity by name (and optionally type)."""
    db = _get_db()
    if entity_type:
        row = db.read_one(
            "SELECT * FROM entities WHERE name = ? AND type = ?",
            (name, entity_type),
        )
    else:
        row = db.read_one(
            "SELECT * FROM entities WHERE name = ? ORDER BY updated_at DESC",
            (name,),
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


def search_entities(query: str, entity_type: str | None = None, limit: int = 10) -> list[dict]:
    """Search entities by name (LIKE match)."""
    db = _get_db()
    if entity_type:
        rows = db.read(
            "SELECT * FROM entities WHERE name LIKE ? AND type = ? ORDER BY updated_at DESC LIMIT ?",
            (f"%{query}%", entity_type, limit),
        )
    else:
        rows = db.read(
            "SELECT * FROM entities WHERE name LIKE ? ORDER BY updated_at DESC LIMIT ?",
            (f"%{query}%", limit),
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
    source_name: str, source_type: str,
    target_name: str, target_type: str,
    relation_type: str, properties: dict | None = None,
) -> int:
    """Add a relation between two entities (creates entities if needed)."""
    source_id = add_entity(source_name, source_type)
    target_id = add_entity(target_name, target_type)

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
            "INSERT INTO relations (source_id, target_id, relation_type, properties, created_at) VALUES (?, ?, ?, ?, ?)",
            (source_id, target_id, relation_type, props, now),
        )
        return cursor.lastrowid

    return db.write_tx(_upsert)


def get_connections(entity_name: str, depth: int = 1) -> list[dict]:
    """Get all connections for an entity (1 or 2 hops)."""
    db = _get_db()
    entity = get_entity(entity_name)
    if not entity:
        return []

    eid = entity["id"]

    rows = db.read(
        """
        SELECT e2.name, e2.type, r.relation_type, 'outgoing' as direction
        FROM relations r
        JOIN entities e2 ON e2.id = r.target_id
        WHERE r.source_id = ?
        UNION ALL
        SELECT e2.name, e2.type, r.relation_type, 'incoming' as direction
        FROM relations r
        JOIN entities e2 ON e2.id = r.source_id
        WHERE r.target_id = ?
        """,
        (eid, eid),
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


def get_graph_context(query: str, limit: int = 5) -> str:
    """Get knowledge graph context relevant to a query.

    Searches entities matching the query and returns their connections
    as a formatted string for LLM context injection.
    """
    entities = search_entities(query, limit=limit)
    if not entities:
        return ""

    parts = []
    for entity in entities:
        connections = get_connections(entity["name"])
        if connections:
            conn_text = ", ".join(
                f"{c['relation']}→{c['entity']}({c['entity_type']})"
                for c in connections[:5]
            )
            parts.append(f"{entity['name']} ({entity['type']}): {conn_text}")
        else:
            parts.append(f"{entity['name']} ({entity['type']})")

    return "\n".join(parts)


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
