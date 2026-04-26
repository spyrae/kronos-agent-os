"""Performance API — per-agent performance metrics."""

import json
import logging
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Depends

from dashboard.auth import verify_token
from kronos.config import settings

router = APIRouter(prefix="/api/performance", tags=["performance"], dependencies=[Depends(verify_token)])
log = logging.getLogger("kronos.dashboard.performance")


@router.get("/agents")
async def get_agent_performance():
    """Per-agent performance metrics: score, OPS, latency."""
    audit_file = Path(settings.db_path).parent / "logs" / "audit.jsonl"
    reg_file = Path(settings.db_path).parent / "agent_registry.json"

    registry = {}
    if reg_file.exists():
        registry = json.loads(reg_file.read_text())

    agent_data: dict[str, dict] = defaultdict(lambda: {
        "latencies": [], "ops": 0, "write_latencies": [], "read_latencies": [],
    })

    if audit_file.exists():
        with open(audit_file) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                agent = entry.get("agent", entry.get("tier", "unknown"))
                duration = entry.get("duration_ms", 0)
                agent_data[agent]["ops"] += 1
                if duration:
                    agent_data[agent]["latencies"].append(duration)
                    input_text = (entry.get("input_preview", "") or "").lower()
                    if "write" in input_text or "save" in input_text:
                        agent_data[agent]["write_latencies"].append(duration)
                    else:
                        agent_data[agent]["read_latencies"].append(duration)

    agents = []
    all_names = set(list(registry.keys()) + list(agent_data.keys()))

    for name in sorted(all_names):
        data = agent_data.get(name, {"latencies": [], "ops": 0, "write_latencies": [], "read_latencies": []})
        lats = data["latencies"]
        w_lats = data["write_latencies"]
        r_lats = data["read_latencies"]

        avg_lat = sum(lats) / max(len(lats), 1) if lats else 0
        score = round(max(0, min(100, 100 - avg_lat / 50)), 1)

        enabled = registry.get(name, {}).get("enabled", True)
        agents.append({
            "name": name,
            "score": score,
            "ops": data["ops"],
            "write_latency_ms": round(sum(w_lats) / max(len(w_lats), 1), 1) if w_lats else 0,
            "read_latency_ms": round(sum(r_lats) / max(len(r_lats), 1), 1) if r_lats else 0,
            "status": "running" if enabled else "stopped",
        })

    return {"agents": agents}
