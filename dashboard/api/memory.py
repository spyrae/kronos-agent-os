"""Memory API — inspect and manage KAOS memory stores."""

import json
import logging
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from dashboard.auth import verify_token
from kronos.config import settings
from kronos.memory.store import add_memories, get_all_memories, get_memory, search_memories

router = APIRouter(prefix="/api/memory", tags=["memory"], dependencies=[Depends(verify_token)])
log = logging.getLogger("kronos.dashboard.memory")


def _sqlite_rows(db_path: Path, query: str, params: tuple = ()) -> list[dict]:
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(query, params).fetchall()]
    except sqlite3.Error:
        return []


def _sqlite_exec(db_path: Path, query: str, params: tuple = ()) -> int:
    if not db_path.exists():
        return 0
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute(query, params)
            conn.commit()
            return cursor.rowcount if cursor.rowcount is not None else 0
    except sqlite3.Error:
        return 0


def _sqlite_count(db_path: Path, table: str) -> int:
    rows = _sqlite_rows(db_path, f"SELECT COUNT(*) AS total FROM {table}")
    return int(rows[0]["total"]) if rows else 0


def _record(
    *,
    record_id: str,
    record_type: str,
    source: str,
    memory: str,
    created_at: str = "",
    updated_at: str = "",
    user_id: str = "",
    session_id: str = "",
    template: str = "",
    metadata: dict | None = None,
    recall_reason: str = "",
) -> dict:
    return {
        "id": record_id,
        "type": record_type,
        "source": source,
        "memory": memory,
        "created_at": created_at,
        "updated_at": updated_at or created_at,
        "user_id": user_id,
        "session_id": session_id,
        "template": template or settings.agent_name,
        "metadata": metadata or {},
        "recall_reason": recall_reason,
    }


def _load_fts_records() -> list[dict]:
    db_path = Path(settings.db_dir) / "memory_fts.db"
    rows = _sqlite_rows(
        db_path,
        """
        SELECT id, user_id, content, COALESCE(source, 'mem0') AS source,
               created_at, mem0_id,
               COALESCE(relevance, 1.0) AS relevance,
               COALESCE(tier, 'active') AS tier,
               COALESCE(last_accessed, created_at) AS last_accessed
        FROM memory_facts
        ORDER BY created_at DESC
        """,
    )
    records = []
    for row in rows:
        records.append(_record(
            record_id=f"fts:{row['id']}",
            record_type="fact",
            source=row["source"],
            user_id=row["user_id"],
            memory=row["content"],
            created_at=row["created_at"],
            updated_at=row["last_accessed"],
            metadata={
                "tier": row["tier"],
                "relevance": row["relevance"],
                "mem0_id": row.get("mem0_id") or "",
            },
            recall_reason=f"FTS exact recall, tier={row['tier']}, relevance={round(float(row['relevance'] or 0), 3)}",
        ))
    return records


def _load_shared_records() -> list[dict]:
    db_path = Path(settings.swarm_db_path)
    rows = _sqlite_rows(
        db_path,
        """
        SELECT id, user_id, fact, source_agent, created_at, last_accessed_at, access_count
        FROM shared_user_facts
        ORDER BY created_at DESC
        """,
    )
    return [
        _record(
            record_id=f"shared:{row['id']}",
            record_type="shared_fact",
            source=row["source_agent"],
            user_id=row["user_id"],
            memory=row["fact"],
            created_at=str(row["created_at"]),
            updated_at=str(row["last_accessed_at"]),
            metadata={"access_count": row["access_count"]},
            recall_reason="Shared coordination fact visible across agents.",
        )
        for row in rows
    ]


def _load_kg_records() -> list[dict]:
    db_path = Path(settings.db_dir) / "knowledge_graph.db"
    entity_rows = _sqlite_rows(
        db_path,
        "SELECT id, name, type, properties, created_at, updated_at FROM entities ORDER BY updated_at DESC",
    )
    relation_rows = _sqlite_rows(
        db_path,
        """
        SELECT r.id, r.relation_type, r.properties, r.created_at,
               s.name AS source_name, s.type AS source_type,
               t.name AS target_name, t.type AS target_type
        FROM relations r
        LEFT JOIN entities s ON s.id = r.source_id
        LEFT JOIN entities t ON t.id = r.target_id
        ORDER BY r.created_at DESC
        """,
    )
    records = []
    for row in entity_rows:
        records.append(_record(
            record_id=f"entity:{row['id']}",
            record_type="entity",
            source="knowledge_graph",
            memory=f"{row['name']} ({row['type']})",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata={"entity_type": row["type"], "properties": row.get("properties") or "{}"},
            recall_reason="Knowledge graph entity can be recalled by name/type.",
        ))
    for row in relation_rows:
        records.append(_record(
            record_id=f"relation:{row['id']}",
            record_type="relation",
            source="knowledge_graph",
            memory=f"{row.get('source_name') or '?'} - {row['relation_type']} -> {row.get('target_name') or '?'}",
            created_at=row["created_at"],
            updated_at=row["created_at"],
            metadata={
                "source_type": row.get("source_type") or "",
                "target_type": row.get("target_type") or "",
                "properties": row.get("properties") or "{}",
            },
            recall_reason="Knowledge graph relation can be recalled for relationship context.",
        ))
    return records


def _load_session_records() -> list[dict]:
    db_path = Path(settings.db_path)
    rows = _sqlite_rows(
        db_path,
        "SELECT thread_id, messages, updated_at FROM sessions ORDER BY updated_at DESC",
    )
    records = []
    for row in rows:
        try:
            messages = json.loads(row["messages"])
        except json.JSONDecodeError:
            messages = []
        preview = ""
        for msg in reversed(messages):
            preview = str(msg.get("content", ""))
            if preview:
                break
        records.append(_record(
            record_id=f"session:{row['thread_id']}",
            record_type="session",
            source="session_store",
            session_id=row["thread_id"],
            memory=preview or f"{len(messages)} messages",
            created_at=row["updated_at"],
            updated_at=row["updated_at"],
            metadata={"message_count": len(messages)},
            recall_reason="Session history is used for recent conversation continuity.",
        ))
    return records


def _load_memory_records() -> list[dict]:
    records = _load_fts_records()
    records.extend(_load_shared_records())
    records.extend(_load_kg_records())
    records.extend(_load_session_records())
    return sorted(records, key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)


@router.get("/status")
async def memory_status():
    """Get memory system status."""
    fts_db = Path(settings.db_dir) / "memory_fts.db"
    kg_db = Path(settings.db_dir) / "knowledge_graph.db"
    swarm_db = Path(settings.swarm_db_path)
    session_db = Path(settings.db_path)
    counts = {
        "facts": _sqlite_count(fts_db, "memory_facts"),
        "entities": _sqlite_count(kg_db, "entities"),
        "relations": _sqlite_count(kg_db, "relations"),
        "shared_facts": _sqlite_count(swarm_db, "shared_user_facts"),
        "sessions": _sqlite_count(session_db, "sessions"),
    }
    try:
        mem = get_memory()
        all_mems = mem.get_all(user_id="")
        count = len(all_mems.get("results", []))
        return {
            "status": "ok",
            "total_memories": count + counts["facts"] + counts["shared_facts"] + counts["entities"] + counts["relations"] + counts["sessions"],
            "qdrant": "connected",
            "counts": counts,
            "stores": {
                "fts": str(fts_db),
                "knowledge_graph": str(kg_db),
                "swarm": str(swarm_db),
                "sessions": str(session_db),
            },
        }
    except Exception as e:
        total = sum(counts.values())
        return {
            "status": "degraded" if total else "error",
            "error": str(e),
            "total_memories": total,
            "qdrant": "unavailable",
            "counts": counts,
        }


@router.get("/all")
async def list_memories(user_id: str = ""):
    """List all memories for a user."""
    try:
        mem = get_memory()
        result = mem.get_all(user_id=user_id)
        memories = []
        for item in result.get("results", []):
            memories.append({
                "id": item.get("id", ""),
                "memory": item.get("memory", ""),
                "created_at": item.get("created_at", ""),
                "updated_at": item.get("updated_at", ""),
            })
        return {"memories": memories, "total": len(memories)}
    except Exception as e:
        return {"memories": [], "total": 0, "error": str(e)}


@router.get("/records")
async def list_memory_records(
    query: str = "",
    type: str = Query("all"),
    source: str = Query("all"),
    session: str = Query("all"),
    limit: int = Query(200, le=1000),
):
    """List inspectable records across FTS, KG, shared facts, and sessions."""
    records = _load_memory_records()

    if query:
        q = query.lower()
        records = [
            item for item in records
            if q in item.get("memory", "").lower()
            or q in json.dumps(item.get("metadata", {}), ensure_ascii=False).lower()
        ]
    if type != "all":
        records = [item for item in records if item.get("type") == type]
    if source != "all":
        records = [item for item in records if item.get("source") == source]
    if session != "all":
        records = [item for item in records if item.get("session_id") == session]

    filters = {
        "types": sorted({item.get("type", "") for item in records if item.get("type")}),
        "sources": sorted({item.get("source", "") for item in records if item.get("source")}),
        "sessions": sorted({item.get("session_id", "") for item in records if item.get("session_id")}),
    }
    return {"records": records[:limit], "total": len(records), "filters": filters}


@router.delete("/records/{record_id:path}")
async def delete_memory_record(record_id: str):
    """Delete a single memory record by inspector ID."""
    if ":" not in record_id:
        raise HTTPException(400, "record_id must be prefixed")

    prefix, raw_id = record_id.split(":", 1)
    deleted = 0
    if prefix == "fts":
        db_path = Path(settings.db_dir) / "memory_fts.db"
        deleted += _sqlite_exec(db_path, "DELETE FROM memory_fts WHERE rowid = ?", (raw_id,))
        deleted += _sqlite_exec(db_path, "DELETE FROM memory_facts WHERE id = ?", (raw_id,))
    elif prefix == "shared":
        deleted += _sqlite_exec(Path(settings.swarm_db_path), "DELETE FROM shared_user_facts WHERE id = ?", (raw_id,))
    elif prefix == "entity":
        db_path = Path(settings.db_dir) / "knowledge_graph.db"
        _sqlite_exec(db_path, "DELETE FROM relations WHERE source_id = ? OR target_id = ?", (raw_id, raw_id))
        deleted += _sqlite_exec(db_path, "DELETE FROM entities WHERE id = ?", (raw_id,))
    elif prefix == "relation":
        deleted += _sqlite_exec(Path(settings.db_dir) / "knowledge_graph.db", "DELETE FROM relations WHERE id = ?", (raw_id,))
    elif prefix == "session":
        deleted += _sqlite_exec(Path(settings.db_path), "DELETE FROM sessions WHERE thread_id = ?", (raw_id,))
    else:
        raise HTTPException(400, f"Unsupported memory record type: {prefix}")

    return {"ok": deleted > 0, "deleted": deleted, "record_id": record_id}


class ResetMemory(BaseModel):
    scope: str
    confirm: bool = False


@router.post("/reset")
async def reset_memory(body: ResetMemory):
    """Reset a memory store. Requires explicit confirmation."""
    if not body.confirm:
        raise HTTPException(400, "confirm=true is required")
    if body.scope not in {"facts", "shared", "knowledge_graph", "sessions", "all"}:
        raise HTTPException(400, "scope must be facts, shared, knowledge_graph, sessions, or all")

    deleted = 0
    if body.scope in {"facts", "all"}:
        db_path = Path(settings.db_dir) / "memory_fts.db"
        deleted += _sqlite_exec(db_path, "DELETE FROM memory_fts")
        deleted += _sqlite_exec(db_path, "DELETE FROM memory_facts")
    if body.scope in {"shared", "all"}:
        db_path = Path(settings.swarm_db_path)
        deleted += _sqlite_exec(db_path, "DELETE FROM shared_user_facts_fts")
        deleted += _sqlite_exec(db_path, "DELETE FROM shared_user_facts")
    if body.scope in {"knowledge_graph", "all"}:
        db_path = Path(settings.db_dir) / "knowledge_graph.db"
        deleted += _sqlite_exec(db_path, "DELETE FROM relations")
        deleted += _sqlite_exec(db_path, "DELETE FROM entities")
    if body.scope in {"sessions", "all"}:
        deleted += _sqlite_exec(Path(settings.db_path), "DELETE FROM sessions")

    log.info("Memory reset: scope=%s deleted=%d", body.scope, deleted)
    return {"ok": True, "scope": body.scope, "deleted": deleted}


class SearchQuery(BaseModel):
    query: str
    user_id: str = ""
    limit: int = 5


@router.post("/search")
async def search(body: SearchQuery):
    """Search memories."""
    results = search_memories(body.query, user_id=body.user_id, limit=body.limit)
    return {"results": results}


class AddMemory(BaseModel):
    text: str
    user_id: str = ""


@router.post("/add")
async def add(body: AddMemory):
    """Manually add a memory."""
    try:
        messages = [{"role": "user", "content": body.text}]
        add_memories(messages, user_id=body.user_id)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/test")
async def test_memory():
    """Run a full memory test: add → search → verify."""
    test_user = "test-memory-check"
    results = {"steps": []}

    # Step 1: Add
    try:
        messages = [
            {"role": "user", "content": "My favorite language is Python and I live in Bali"},
            {"role": "assistant", "content": "Got it! Python fan in Bali."},
        ]
        add_memories(messages, user_id=test_user)
        results["steps"].append({"step": "add", "status": "ok"})
    except Exception as e:
        results["steps"].append({"step": "add", "status": "error", "error": str(e)})
        return results

    # Step 2: Search
    try:
        found = search_memories("What programming language?", user_id=test_user, limit=3)
        results["steps"].append({"step": "search", "status": "ok", "found": found})
    except Exception as e:
        results["steps"].append({"step": "search", "status": "error", "error": str(e)})

    # Step 3: Get all
    try:
        all_mems = get_all_memories(test_user)
        results["steps"].append({"step": "get_all", "status": "ok", "count": len(all_mems), "memories": all_mems})
    except Exception as e:
        results["steps"].append({"step": "get_all", "status": "error", "error": str(e)})

    results["overall"] = "ok" if all(s["status"] == "ok" for s in results["steps"]) else "failed"
    return results
