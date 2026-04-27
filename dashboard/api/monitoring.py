"""Monitoring API — stats, cost, request history."""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

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


SAFE_TRIGGER_JOBS = {"heartbeat", "swarm-retention"}


def _history_file() -> Path:
    return Path(settings.db_path).parent / "logs" / "cron_runs.jsonl"


def _load_run_history() -> list[dict]:
    path = _history_file()
    if not path.exists():
        return []
    rows = []
    with open(path) as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    rows.reverse()
    return rows


def _job_owner(name: str) -> str:
    if name.startswith(("analytics", "competitor")):
        return "nexus"
    return settings.agent_name


def _job_capabilities(name: str) -> list[str]:
    caps = []
    if any(part in name for part in ("digest", "pulse", "weekly", "review", "monitor", "scout")):
        caps.append("notifications")
    if any(part in name for part in ("analytics", "competitor", "market")):
        caps.append("research")
    if any(part in name for part in ("expense", "budget")):
        caps.append("finance")
    if any(part in name for part in ("skill", "self-improve", "sleep", "user-model")):
        caps.append("memory")
    if "swarm" in name:
        caps.append("coordination")
    return caps or ["runtime"]


def _format_schedule(job) -> str:
    if job.interval_seconds:
        minutes = round(job.interval_seconds / 60)
        return f"every {minutes}m" if minutes < 60 else f"every {round(minutes / 60)}h"
    if job.cron_hour is not None and job.cron_weekday is not None:
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return f"{days[job.cron_weekday]} {job.cron_hour:02d}:00 UTC"
    if job.cron_hour is not None:
        return f"daily {job.cron_hour:02d}:00 UTC"
    return "manual"


def _next_run(job) -> str | None:
    if not job.enabled:
        return None
    now = datetime.now(UTC)
    if job.interval_seconds:
        base = datetime.fromtimestamp(job.last_run, tz=UTC) if job.last_run else now
        next_dt = base + timedelta(seconds=job.interval_seconds)
        return max(next_dt, now).isoformat()
    if job.cron_hour is not None:
        candidate = now.replace(hour=job.cron_hour, minute=0, second=0, microsecond=0)
        if job.cron_weekday is not None:
            days_ahead = (job.cron_weekday - now.weekday()) % 7
            candidate = candidate + timedelta(days=days_ahead)
            if candidate <= now:
                candidate = candidate + timedelta(days=7)
        elif candidate <= now:
            candidate = candidate + timedelta(days=1)
        return candidate.isoformat()
    return None


def _job_payload(name: str, job) -> dict:
    last_run = datetime.fromtimestamp(job.last_run, tz=UTC).isoformat() if job.last_run else None
    status = "running" if job._running else "enabled" if job.enabled else "paused"
    return {
        "name": name,
        "enabled": job.enabled,
        "running": job._running,
        "status": status,
        "schedule": _format_schedule(job),
        "last_run": last_run,
        "next_run": _next_run(job),
        "owner": _job_owner(name),
        "capabilities": _job_capabilities(name),
        "safe_controls": {
            "pause": True,
            "resume": True,
            "trigger_now": name in SAFE_TRIGGER_JOBS,
        },
    }


def _demo_jobs() -> list[dict]:
    return [{
        "name": "demo-daily-brief",
        "enabled": False,
        "running": False,
        "status": "demo",
        "schedule": "daily 09:00 UTC",
        "last_run": None,
        "next_run": None,
        "owner": "demo",
        "capabilities": ["memory", "notifications"],
        "safe_controls": {"pause": False, "resume": False, "trigger_now": False},
    }]


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


@router.get("/jobs")
async def get_jobs():
    """List scheduled jobs with monitor metadata."""
    if not _scheduler:
        return {"jobs": _demo_jobs(), "scheduler_attached": False}

    jobs = [_job_payload(name, job) for name, job in sorted(_scheduler.jobs.items())]
    return {"jobs": jobs, "scheduler_attached": True}


@router.get("/jobs/history")
async def get_job_history(job: str = Query("all"), status: str = Query("all"), limit: int = Query(100, le=500)):
    """List persisted cron run history."""
    history = _load_run_history()
    if job != "all":
        history = [item for item in history if item.get("job") == job]
    if status != "all":
        history = [item for item in history if item.get("status") == status]
    return {"runs": history[:limit], "total": len(history)}


@router.post("/jobs/{name}/pause")
async def pause_job(name: str):
    if not _scheduler or name not in _scheduler.jobs:
        raise HTTPException(404, f"Job not found: {name}")
    _scheduler.jobs[name].enabled = False
    return {"ok": True, "job": _job_payload(name, _scheduler.jobs[name])}


@router.post("/jobs/{name}/resume")
async def resume_job(name: str):
    if not _scheduler or name not in _scheduler.jobs:
        raise HTTPException(404, f"Job not found: {name}")
    _scheduler.jobs[name].enabled = True
    return {"ok": True, "job": _job_payload(name, _scheduler.jobs[name])}


@router.post("/jobs/{name}/trigger")
async def trigger_job(name: str):
    if not _scheduler or name not in _scheduler.jobs:
        raise HTTPException(404, f"Job not found: {name}")
    if name not in SAFE_TRIGGER_JOBS:
        raise HTTPException(403, f"Manual trigger is disabled for job: {name}")
    job = _scheduler.jobs[name]
    if job._running:
        raise HTTPException(409, f"Job already running: {name}")
    asyncio.create_task(_scheduler._run_job(job))
    return {"ok": True, "job": _job_payload(name, job), "triggered_at": time.time()}


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
        {"date": date, "cost_usd": round(daily[date], 4), "requests": daily_count[date]}
        for date in sorted(daily.keys())[-days:]
    ]
    return {"days": result}
