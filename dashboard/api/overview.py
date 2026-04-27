"""Overview API — aggregated KPIs and control-room data."""

import hashlib
import json
import logging
import sqlite3
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends

from dashboard.auth import verify_token
from kronos.config import settings

router = APIRouter(prefix="/api/overview", tags=["overview"], dependencies=[Depends(verify_token)])
log = logging.getLogger("kronos.dashboard.overview")

_start_time = time.time()


def _load_audit_entries() -> list[dict]:
    audit_file = Path(settings.db_path).parent / "logs" / "audit.jsonl"
    if not audit_file.exists():
        return []
    entries = []
    with open(audit_file) as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _load_tool_events() -> list[dict]:
    tool_file = Path(settings.db_path).parent / "logs" / "tool_calls.jsonl"
    if not tool_file.exists():
        return []
    entries = []
    with open(tool_file) as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _load_registry() -> dict:
    reg_file = Path(settings.db_path).parent / "agent_registry.json"
    if reg_file.exists():
        return json.loads(reg_file.read_text())
    try:
        from dashboard.api.agents import DEFAULT_REGISTRY
        return dict(DEFAULT_REGISTRY)
    except Exception:
        return {}


def _get_scheduler():
    try:
        from dashboard.api import monitoring
        return monitoring._scheduler
    except Exception:
        return None


def _sqlite_scalar(db_path: Path, query: str, default: int = 0) -> int:
    if not db_path.exists():
        return default
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(query).fetchone()
            return int(row[0] or 0) if row else default
    except sqlite3.Error:
        return default


def _classify_activity(entry: dict) -> str:
    text = (entry.get("input_preview", "") or "").lower()
    output = (entry.get("output_preview", "") or "").lower()

    if entry.get("blocked"):
        return "APPROVAL"
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


def _activity_id(entry: dict) -> str:
    digest = hashlib.md5(json.dumps(entry, sort_keys=True, default=str).encode()).hexdigest()[:12]
    return f"act_{digest}"


def _format_ts(ts: float | int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(float(ts), tz=UTC).isoformat()


def _format_schedule(job) -> str:
    interval = getattr(job, "interval_seconds", None)
    cron_hour = getattr(job, "cron_hour", None)
    cron_weekday = getattr(job, "cron_weekday", None)
    if interval:
        if interval < 3600:
            return f"every {round(interval / 60)}m"
        return f"every {round(interval / 3600)}h"
    if cron_hour is not None and cron_weekday is not None:
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        day = days[cron_weekday] if 0 <= cron_weekday < len(days) else str(cron_weekday)
        return f"{day} {cron_hour:02d}:00 UTC"
    if cron_hour is not None:
        return f"daily {cron_hour:02d}:00 UTC"
    return "manual"


def _capability_status() -> dict:
    capabilities = [
        {
            "key": "dynamic_tools",
            "label": "Dynamic Tools",
            "status": "enabled" if settings.enable_dynamic_tools else "blocked",
            "risk": "high",
        },
        {
            "key": "dynamic_tool_sandbox",
            "label": "Tool Sandbox",
            "status": "enabled" if settings.require_dynamic_tool_sandbox else "blocked",
            "risk": "protective",
        },
        {
            "key": "mcp_gateway_management",
            "label": "MCP Gateway Management",
            "status": "enabled" if settings.enable_mcp_gateway_management else "blocked",
            "risk": "high",
        },
        {
            "key": "dynamic_mcp_servers",
            "label": "Dynamic MCP Servers",
            "status": "enabled" if settings.enable_dynamic_mcp_servers else "blocked",
            "risk": "high",
        },
        {
            "key": "server_ops",
            "label": "Server Ops",
            "status": "enabled" if settings.enable_server_ops else "blocked",
            "risk": "critical",
        },
    ]
    enabled = sum(1 for item in capabilities if item["status"] == "enabled")
    blocked = sum(1 for item in capabilities if item["status"] == "blocked")
    risky_enabled = sum(
        1 for item in capabilities
        if item["status"] == "enabled" and item["risk"] in {"high", "critical"}
    )
    sandbox_blocked = not settings.require_dynamic_tool_sandbox

    return {
        "posture": "strict" if risky_enabled == 0 and not sandbox_blocked else "open",
        "enabled": enabled,
        "blocked": blocked,
        "warnings": risky_enabled + (1 if sandbox_blocked else 0),
        "items": capabilities,
    }


def _recent_sessions(entries: list[dict], limit: int = 5) -> list[dict]:
    sessions: dict[str, dict] = {}
    for entry in entries:
        session_id = str(entry.get("session_id") or "unknown")
        item = sessions.setdefault(
            session_id,
            {
                "id": session_id,
                "agent": entry.get("agent", entry.get("tier", "unknown")),
                "requests": 0,
                "last_seen": "",
                "summary": "",
            },
        )
        item["requests"] += 1
        ts = entry.get("ts", "")
        if ts >= item["last_seen"]:
            item["last_seen"] = ts
            item["agent"] = entry.get("agent", entry.get("tier", item["agent"]))
            item["summary"] = entry.get("input_preview") or entry.get("output_preview") or ""
    return sorted(sessions.values(), key=lambda item: item["last_seen"], reverse=True)[:limit]


def _recent_activity(entries: list[dict], limit: int = 8) -> list[dict]:
    activity = []
    for entry in reversed(entries[-100:]):
        description = entry.get("input_preview") or entry.get("output_preview") or ""
        activity.append({
            "id": _activity_id(entry),
            "type": _classify_activity(entry),
            "agent": entry.get("agent", entry.get("tier", "unknown")),
            "description": description,
            "timestamp": entry.get("ts", ""),
            "duration_ms": entry.get("duration_ms"),
            "cost_usd": entry.get("approx_cost_usd"),
        })
        if len(activity) >= limit:
            break
    return activity


def _recent_tool_activity(tool_events: list[dict], fallback_entries: list[dict], limit: int = 8) -> list[dict]:
    if not tool_events:
        return _recent_activity(fallback_entries, limit=limit)

    activity = []
    for entry in reversed(tool_events[-100:]):
        description = entry.get("result_summary") or entry.get("args_summary") or entry.get("tool") or ""
        activity.append({
            "id": _activity_id(entry),
            "type": str(entry.get("event", "tool")).upper(),
            "agent": entry.get("agent", "unknown"),
            "description": description,
            "timestamp": entry.get("ts", ""),
            "duration_ms": entry.get("duration_ms"),
            "cost_usd": entry.get("cost_usd"),
        })
        if len(activity) >= limit:
            break
    return activity


def _cron_jobs() -> dict:
    scheduler = _get_scheduler()
    if not scheduler:
        return {"enabled": 0, "running": 0, "total": 0, "items": []}

    items = []
    for name, job in scheduler.jobs.items():
        running = bool(getattr(job, "_running", False))
        enabled = bool(getattr(job, "enabled", False))
        items.append({
            "name": name,
            "enabled": enabled,
            "running": running,
            "status": "running" if running else "enabled" if enabled else "disabled",
            "schedule": _format_schedule(job),
            "last_run": _format_ts(getattr(job, "last_run", 0.0)),
        })

    return {
        "enabled": sum(1 for item in items if item["enabled"]),
        "running": sum(1 for item in items if item["running"]),
        "total": len(items),
        "items": sorted(items, key=lambda item: (item["status"] != "running", item["name"]))[:8],
    }


def _memory_status() -> dict:
    db_dir = Path(settings.db_dir)
    fts_db = db_dir / "memory_fts.db"
    kg_db = db_dir / "knowledge_graph.db"
    qdrant_dir = Path(settings.mem0_qdrant_path)

    fts_facts = _sqlite_scalar(fts_db, "SELECT COUNT(*) FROM memory_facts")
    kg_entities = _sqlite_scalar(kg_db, "SELECT COUNT(*) FROM entities")
    kg_relations = _sqlite_scalar(kg_db, "SELECT COUNT(*) FROM relations")
    qdrant_present = qdrant_dir.exists()
    initialized = db_dir.exists() or fts_db.exists() or kg_db.exists() or qdrant_present

    return {
        "status": "ready" if initialized else "not_initialized",
        "db_dir": str(db_dir),
        "fts_facts": fts_facts,
        "kg_entities": kg_entities,
        "kg_relations": kg_relations,
        "qdrant_present": qdrant_present,
    }


def _coordination_status() -> dict:
    swarm_db = Path(settings.swarm_db_path)
    messages = _sqlite_scalar(swarm_db, "SELECT COUNT(*) FROM swarm_messages")
    active_claims = _sqlite_scalar(swarm_db, "SELECT COUNT(*) FROM reply_claims WHERE state = 'claimed'")
    sent_claims = _sqlite_scalar(swarm_db, "SELECT COUNT(*) FROM reply_claims WHERE state = 'sent'")
    shared_facts = _sqlite_scalar(swarm_db, "SELECT COUNT(*) FROM shared_user_facts")
    duplicate_avoided = _sqlite_scalar(
        swarm_db,
        "SELECT value FROM swarm_metrics WHERE metric = 'duplicate_replies_avoided'",
    )

    return {
        "status": "active" if active_claims else "ready" if swarm_db.exists() else "not_initialized",
        "db_path": str(swarm_db),
        "messages": messages,
        "active_claims": active_claims,
        "sent_claims": sent_claims,
        "shared_facts": shared_facts,
        "duplicate_replies_avoided": duplicate_avoided,
    }


def _approval_status() -> dict:
    try:
        from dashboard.api.config import _load_approvals
        approvals = _load_approvals()
    except Exception:
        approvals = []
    pending = [item for item in approvals if item.get("status") == "pending"]
    return {
        "pending": len(pending),
        "recent": approvals[:5],
    }


@router.get("/kpi")
async def get_kpi():
    """Aggregated KPI data for overview page."""
    from datetime import datetime, timedelta

    registry = _load_registry()
    active_agents = sum(1 for a in registry.values() if a.get("enabled", True))

    entries = _load_audit_entries()
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    today_ops = sum(1 for e in entries if e.get("ts", "").startswith(today))
    yesterday_ops = sum(1 for e in entries if e.get("ts", "").startswith(yesterday))
    ops_change = round(((today_ops - yesterday_ops) / max(yesterday_ops, 1)) * 100) if yesterday_ops else 0

    # Average score from latencies
    latencies = [e.get("duration_ms", 0) for e in entries if e.get("ts", "").startswith(today) and e.get("duration_ms")]
    avg_latency = sum(latencies) / max(len(latencies), 1)
    avg_score = round(max(0, min(100, 100 - avg_latency / 50)), 1)

    # Memory count
    try:
        from kronos.memory.store import get_memory
        mem = get_memory()
        all_mems = mem.get_all(user_id="")
        memories_count = len(all_mems.get("results", []))
    except Exception:
        memories_count = 0

    # Storage estimate
    audit_file = Path(settings.db_path).parent / "logs" / "audit.jsonl"
    storage_kb = round(audit_file.stat().st_size / 1024, 1) if audit_file.exists() else 0

    return {
        "active_agents": active_agents,
        "active_agents_total": len(registry),
        "active_agents_change": 0,
        "total_ops": today_ops,
        "total_ops_change": ops_change,
        "avg_score": avg_score,
        "avg_score_change": 2,
        "uptime_seconds": int(time.time() - _start_time),
        "memories_count": memories_count,
        "storage_kb": storage_kb,
    }


@router.get("/operations")
async def get_operations():
    """Per-agent operations breakdown for bar chart."""
    entries = _load_audit_entries()
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")

    agents: dict[str, dict[str, int]] = defaultdict(lambda: {"writes": 0, "reads": 0, "searches": 0})

    for e in entries:
        if not e.get("ts", "").startswith(today):
            continue
        agent = e.get("agent", e.get("tier", "unknown"))
        input_text = (e.get("input_preview", "") or "").lower()
        if "search" in input_text or "find" in input_text:
            agents[agent]["searches"] += 1
        elif "write" in input_text or "save" in input_text or "add" in input_text:
            agents[agent]["writes"] += 1
        else:
            agents[agent]["reads"] += 1

    result = [{"name": name, **ops} for name, ops in agents.items()]
    return {"agents": result}


@router.get("/control-room")
async def get_control_room():
    """First-screen status for the KAOS dashboard control room."""
    registry = _load_registry()
    entries = _load_audit_entries()
    tool_events = _load_tool_events()
    safety = _capability_status()
    approvals = _approval_status()
    jobs = _cron_jobs()
    memory = _memory_status()
    coordination = _coordination_status()

    enabled_agents = sum(1 for item in registry.values() if item.get("enabled", True))
    primary_agent = settings.agent_name
    workspace = settings.workspace_path or f"workspaces/{settings.agent_name}"

    return {
        "runtime": {
            "agent": primary_agent,
            "status": "running",
            "uptime_seconds": int(time.time() - _start_time),
            "workspace": workspace,
            "db_dir": settings.db_dir,
            "audit_entries": len(entries),
            "tool_events": len(tool_events),
        },
        "agents": {
            "enabled": enabled_agents,
            "total": len(registry),
            "primary": primary_agent,
        },
        "safety": safety,
        "approvals": {
            "pending": approvals["pending"],
            "recent": approvals["recent"],
            "policy": safety["posture"],
        },
        "jobs": jobs,
        "memory": memory,
        "coordination": coordination,
        "sessions": _recent_sessions(entries),
        "recent_activity": _recent_tool_activity(tool_events, entries),
    }
