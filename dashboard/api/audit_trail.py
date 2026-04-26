"""Audit Trail API — typed events timeline."""

import json
import hashlib
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


@router.get("/events")
async def get_events(
    type: str = Query("all"),
    limit: int = Query(50, le=500),
    offset: int = Query(0),
):
    """Get typed audit events."""
    audit_file = Path(settings.db_path).parent / "logs" / "audit.jsonl"
    if not audit_file.exists():
        return {"events": [], "counts": {}, "total": 0}

    entries = []
    with open(audit_file) as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

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
