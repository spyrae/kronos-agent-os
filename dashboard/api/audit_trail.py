"""Audit Trail API — typed events timeline."""

import hashlib
import json
import logging
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from dashboard.auth import verify_token
from kronos.config import settings

router = APIRouter(prefix="/api/audit-trail", tags=["audit-trail"], dependencies=[Depends(verify_token)])
log = logging.getLogger("kronos.dashboard.audit_trail")


def _classify_event(entry: dict) -> str:
    """Classify an audit entry into an event type."""
    text = (entry.get("input_preview", "") or "").lower()
    output = (entry.get("output_preview", "") or "").lower()

    if entry.get("error"):
        return "ERROR"
    if "crash" in text or "crash" in output:
        return "CRASH"
    if "recover" in text or "restore" in output:
        return "RECOVERY"
    if "search" in text or "find" in text or "query" in text:
        return "SEARCH"
    if "write" in text or "save" in text or "add" in text or "update" in text:
        return "WRITE"
    return "DECISION"


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []

    entries = []
    with open(path) as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


@router.get("/events")
async def get_events(
    type: str = Query("all"),
    limit: int = Query(50, le=500),
    offset: int = Query(0),
):
    """Get typed audit events."""
    audit_file = Path(settings.db_path).parent / "logs" / "audit.jsonl"
    entries = _load_jsonl(audit_file)
    if not entries:
        return {"events": [], "counts": {}, "total": 0}

    entries.reverse()

    counts: dict[str, int] = defaultdict(int)
    events = []

    for entry in entries:
        event_type = _classify_event(entry)
        counts[event_type] += 1

        if type != "all" and event_type != type.upper():
            continue

        evt_id = hashlib.md5(json.dumps(entry, sort_keys=True, default=str).encode()).hexdigest()[:12]
        events.append({
            "id": f"evt_{evt_id}",
            "type": event_type,
            "agent": entry.get("agent", entry.get("tier", "unknown")),
            "description": entry.get("input_preview", "") or entry.get("output_preview", ""),
            "timestamp": entry.get("ts", ""),
            "metadata": {
                "duration_ms": entry.get("duration_ms"),
                "cost_usd": entry.get("approx_cost_usd"),
            },
        })

    total = len(events)
    page = events[offset:offset + limit]

    return {
        "events": page,
        "counts": dict(counts),
        "total": total,
    }


@router.get("/tool-calls")
async def get_tool_calls(
    session: str = Query("all"),
    tool: str = Query("all"),
    status: str = Query("all"),
    capability: str = Query("all"),
    limit: int = Query(50, le=500),
    offset: int = Query(0),
):
    """Get durable tool-call events with dashboard filters."""
    tool_file = Path(settings.db_path).parent / "logs" / "tool_calls.jsonl"
    entries = _load_jsonl(tool_file)
    if not entries:
        return {
            "events": [],
            "counts": {"by_status": {}, "by_capability": {}, "by_tool": {}},
            "filters": {"sessions": [], "tools": [], "capabilities": [], "statuses": []},
            "total": 0,
        }

    entries.reverse()

    counts = {
        "by_status": defaultdict(int),
        "by_capability": defaultdict(int),
        "by_tool": defaultdict(int),
    }
    sessions: set[str] = set()
    tools: set[str] = set()
    capabilities: set[str] = set()
    statuses: set[str] = set()
    filtered = []

    for entry in entries:
        entry_session = str(entry.get("session_id") or "")
        entry_tool = str(entry.get("tool") or "unknown")
        entry_status = str(entry.get("status") or "unknown")
        entry_capability = str(entry.get("capability") or "tools")

        counts["by_status"][entry_status] += 1
        counts["by_capability"][entry_capability] += 1
        counts["by_tool"][entry_tool] += 1
        if entry_session:
            sessions.add(entry_session)
        tools.add(entry_tool)
        capabilities.add(entry_capability)
        statuses.add(entry_status)

        if session != "all" and entry_session != session:
            continue
        if tool != "all" and entry_tool != tool:
            continue
        if status != "all" and entry_status != status:
            continue
        if capability != "all" and entry_capability != capability:
            continue

        event_id = hashlib.md5(json.dumps(entry, sort_keys=True, default=str).encode()).hexdigest()[:12]
        filtered.append({
            "id": f"tool_{event_id}",
            "timestamp": entry.get("ts", ""),
            "event": entry.get("event", ""),
            "status": entry_status,
            "tool": entry_tool,
            "capability": entry_capability,
            "approval_status": entry.get("approval_status", ""),
            "agent": entry.get("agent", "unknown"),
            "session_id": entry_session,
            "thread_id": entry.get("thread_id", ""),
            "source_kind": entry.get("source_kind", ""),
            "call_id": entry.get("call_id", ""),
            "turn": entry.get("turn"),
            "args_summary": entry.get("args_summary", ""),
            "result_summary": entry.get("result_summary", ""),
            "error": bool(entry.get("error", False)),
            "duration_ms": entry.get("duration_ms"),
            "cost_usd": entry.get("cost_usd"),
            "input_tokens": entry.get("input_tokens"),
            "output_tokens": entry.get("output_tokens"),
        })

    total = len(filtered)
    page = filtered[offset:offset + limit]

    return {
        "events": page,
        "counts": {
            "by_status": dict(counts["by_status"]),
            "by_capability": dict(counts["by_capability"]),
            "by_tool": dict(counts["by_tool"]),
        },
        "filters": {
            "sessions": sorted(sessions),
            "tools": sorted(tools),
            "capabilities": sorted(capabilities),
            "statuses": sorted(statuses),
        },
        "total": total,
    }
