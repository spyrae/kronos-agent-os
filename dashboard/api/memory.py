"""Memory API — inspect and manage Mem0 memories."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dashboard.auth import verify_token
from kronos.memory.store import add_memories, get_all_memories, get_memory, search_memories

router = APIRouter(prefix="/api/memory", tags=["memory"], dependencies=[Depends(verify_token)])
log = logging.getLogger("kronos.dashboard.memory")


@router.get("/status")
async def memory_status():
    """Get memory system status."""
    try:
        mem = get_memory()
        all_mems = mem.get_all(user_id="")  # Roman's user ID
        count = len(all_mems.get("results", []))
        return {
            "status": "ok",
            "total_memories": count,
            "qdrant": "connected",
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "total_memories": 0}


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
