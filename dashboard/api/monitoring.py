"""Monitoring API — stats, cost, request history."""

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from dashboard.auth import verify_token
from kronos.audit import get_daily_cost
from kronos.config import settings
from kronos.cron.scheduler import Scheduler

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"], dependencies=[Depends(verify_token)])

# Reference set from server.py
_scheduler: Scheduler | None = None


def set_scheduler(scheduler: Scheduler) -> None:
    global _scheduler
    _scheduler = scheduler


@router.get("/stats")
async def get_stats():
    """Aggregated stats: today's cost, total requests, cron status."""
    cost = get_daily_cost()

    cron_jobs = {}
    if _scheduler:
        for name, job in _scheduler.jobs.items():
            cron_jobs[name] = {
                "enabled": job.enabled,
                "running": job._running,
                "last_run": job.last_run,
            }

    return {
        "cost": cost,
        "cron_jobs": cron_jobs,
    }


@router.get("/requests")
async def get_requests(limit: int = Query(50, le=500), offset: int = Query(0)):
    """Request history from audit.jsonl."""
    audit_file = Path(settings.db_path).parent / "logs" / "audit.jsonl"
    if not audit_file.exists():
        return {"requests": [], "total": 0}

    entries = []
    with open(audit_file) as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Reverse chronological
    entries.reverse()
    total = len(entries)
    page = entries[offset:offset + limit]

    return {"requests": page, "total": total}


@router.get("/cost")
async def get_cost_history(days: int = Query(7, le=90)):
    """Cost aggregation per day."""
    cost_file = Path(settings.db_path).parent / "logs" / "cost.jsonl"
    if not cost_file.exists():
        return {"days": []}

    from collections import defaultdict
    daily: dict[str, float] = defaultdict(float)
    daily_count: dict[str, int] = defaultdict(int)

    with open(cost_file) as f:
        for line in f:
            try:
                entry = json.loads(line)
                date = entry.get("ts", "")[:10]
                daily[date] += entry.get("cost_usd", 0)
                daily_count[date] += 1
            except json.JSONDecodeError:
                continue

    result = [
        {"date": date, "cost_usd": round(cost, 4), "requests": daily_count[date]}
        for date in sorted(daily.keys())[-days:]
    ]
    return {"days": result}
