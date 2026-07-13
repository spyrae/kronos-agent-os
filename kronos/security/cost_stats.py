"""Cost aggregation for the /stats command (roadmap 6.2).

Reads the per-agent cost.jsonl written by kronos.audit. Provides this agent's
breakdown by tier and a swarm-wide breakdown by agent (sibling data dirs).
"""

import json
import time
from collections import defaultdict
from pathlib import Path

from kronos.audit import _get_audit_dir
from kronos.config import settings


def _cutoff(period: str) -> str:
    """Earliest YYYY-MM-DD date (inclusive) counted for the period."""
    if period == "week":
        return time.strftime("%Y-%m-%d", time.gmtime(time.time() - 7 * 86400))
    return time.strftime("%Y-%m-%d")  # today


def _aggregate(cost_file: Path, cutoff: str) -> dict:
    by_tier: dict[str, dict] = defaultdict(lambda: {"requests": 0, "cost": 0.0, "input_tokens": 0, "output_tokens": 0})
    total = {"requests": 0, "cost": 0.0, "input_tokens": 0, "output_tokens": 0}
    if not cost_file.exists():
        return {"total": total, "by_tier": {}}

    with open(cost_file, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("ts", "")[:10] < cutoff:
                continue
            tier = entry.get("tier", "unknown")
            cost = float(entry.get("cost_usd", 0) or 0)
            it = int(entry.get("input_tokens", 0) or 0)
            ot = int(entry.get("output_tokens", 0) or 0)
            for bucket in (by_tier[tier], total):
                bucket["requests"] += 1
                bucket["cost"] += cost
                bucket["input_tokens"] += it
                bucket["output_tokens"] += ot
    return {"total": total, "by_tier": dict(by_tier)}


def cost_report(period: str = "today") -> dict:
    """This agent's cost breakdown for the period (by tier + total)."""
    result = _aggregate(_get_audit_dir() / "cost.jsonl", _cutoff(period))
    return {"period": period, **result}


def swarm_cost_by_agent(period: str = "today") -> dict[str, float]:
    """Total cost per agent across the swarm (sibling data/<agent>/logs dirs)."""
    cutoff = _cutoff(period)
    data_root = Path(settings.db_dir).parent
    out: dict[str, float] = {}
    if not data_root.exists():
        return out
    for agent_dir in sorted(data_root.iterdir()):
        cost_file = agent_dir / "logs" / "cost.jsonl"
        if not cost_file.exists():
            continue
        agg = _aggregate(cost_file, cutoff)
        cost = agg["total"]["cost"]
        if cost > 0 or agg["total"]["requests"] > 0:
            out[agent_dir.name] = round(cost, 4)
    return out
