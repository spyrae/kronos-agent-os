"""Anomalies API — anomaly detection from audit patterns."""

import hashlib
import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends

from dashboard.auth import verify_token
from kronos.config import settings

router = APIRouter(prefix="/api/anomalies", tags=["anomalies"], dependencies=[Depends(verify_token)])
log = logging.getLogger("kronos.dashboard.anomalies")


@router.get("/list")
async def list_anomalies():
    """Detect anomalies from audit.jsonl patterns."""
    audit_file = Path(settings.db_path).parent / "logs" / "audit.jsonl"
    if not audit_file.exists():
        return {"anomalies": [], "summary": {"CRITICAL": 0, "WARNING": 0, "INFO": 0}}

    entries = []
    with open(audit_file) as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    anomalies = []
    now = datetime.now()

    # --- Detect repeat loops: same agent, similar input within 5 min window ---
    agent_recent: dict[str, list[dict]] = defaultdict(list)
    for entry in entries[-200:]:
        agent = entry.get("agent", entry.get("tier", "unknown"))
        agent_recent[agent].append(entry)

    for agent, agent_entries in agent_recent.items():
        if len(agent_entries) < 5:
            continue
        # Check last 10 entries for similarity
        recent = agent_entries[-10:]
        inputs = [e.get("input_preview", "") for e in recent if e.get("input_preview")]
        if len(inputs) >= 5:
            # Simple duplicate detection
            from collections import Counter
            counter = Counter(inputs)
            for text, count in counter.items():
                if count >= 3:
                    aid = hashlib.md5(f"repeat_{agent}_{text}".encode()).hexdigest()[:12]
                    anomalies.append({
                        "id": f"anom_{aid}",
                        "severity": "WARNING",
                        "type": "REPEAT_LOOP",
                        "agent": agent,
                        "description": f"Agent stored similar content {count} times (key: {text[:50]})",
                        "timestamp": recent[-1].get("ts", now.isoformat()),
                    })

    # --- Detect latency spikes ---
    all_latencies = [e.get("duration_ms", 0) for e in entries if e.get("duration_ms")]
    if all_latencies:
        avg_latency = sum(all_latencies) / len(all_latencies)
        threshold = avg_latency * 3

        for entry in entries[-50:]:
            duration = entry.get("duration_ms", 0)
            if duration > threshold and duration > 1000:
                agent = entry.get("agent", entry.get("tier", "unknown"))
                aid = hashlib.md5(f"latency_{agent}_{duration}".encode()).hexdigest()[:12]
                anomalies.append({
                    "id": f"anom_{aid}",
                    "severity": "WARNING",
                    "type": "LATENCY_SPIKE",
                    "agent": agent,
                    "description": f"Latency spike: {duration:.0f}ms (avg: {avg_latency:.0f}ms)",
                    "timestamp": entry.get("ts", now.isoformat()),
                })

    # --- Detect errors/crashes ---
    for entry in entries[-100:]:
        if entry.get("error"):
            agent = entry.get("agent", entry.get("tier", "unknown"))
            aid = hashlib.md5(f"error_{agent}_{entry.get('ts', '')}".encode()).hexdigest()[:12]
            anomalies.append({
                "id": f"anom_{aid}",
                "severity": "CRITICAL",
                "type": "CRASH_LOOP",
                "agent": agent,
                "description": f"Error: {str(entry.get('error', ''))[:80]}",
                "timestamp": entry.get("ts", now.isoformat()),
            })

    # --- Detect idle agents ---
    reg_file = Path(settings.db_path).parent / "agent_registry.json"
    if reg_file.exists():
        registry = json.loads(reg_file.read_text())
        active_agents = {name for name, cfg in registry.items() if cfg.get("enabled", True)}
        agents_with_ops = {e.get("agent", e.get("tier")) for e in entries[-200:]}
        idle = active_agents - agents_with_ops
        for agent in idle:
            aid = hashlib.md5(f"idle_{agent}".encode()).hexdigest()[:12]
            anomalies.append({
                "id": f"anom_{aid}",
                "severity": "INFO",
                "type": "IDLE_ANOMALY",
                "agent": agent,
                "description": "No operations detected while other agents are active",
                "timestamp": now.isoformat(),
            })

    # Deduplicate by id
    seen = set()
    unique = []
    for a in anomalies:
        if a["id"] not in seen:
            seen.add(a["id"])
            unique.append(a)

    summary = {"CRITICAL": 0, "WARNING": 0, "INFO": 0}
    for a in unique:
        summary[a["severity"]] = summary.get(a["severity"], 0) + 1

    return {"anomalies": unique, "summary": summary}
