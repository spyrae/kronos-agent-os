"""Overview API — aggregated KPIs and operations breakdown."""

import json
import time
import logging
from collections import defaultdict
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


def _load_registry() -> dict:
    reg_file = Path(settings.db_path).parent / "agent_registry.json"
    if reg_file.exists():
        return json.loads(reg_file.read_text())
    return {}


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
