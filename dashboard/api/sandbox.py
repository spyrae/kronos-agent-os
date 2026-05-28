"""Sandbox platform API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from dashboard.auth import verify_token
from kronos.tools.sandbox_platform import read_sandbox_records, sandbox_platform_status

router = APIRouter(prefix="/api/sandbox", tags=["sandbox"], dependencies=[Depends(verify_token)])


@router.get("/status")
async def get_status():
    """Return sandbox platform readiness and policy posture."""
    return sandbox_platform_status()


@router.get("/runs")
async def get_runs(
    status: str = Query("all"),
    limit: int = Query(100, le=500),
):
    """Return recent sandbox runs and blocked policy events."""
    records = read_sandbox_records(limit=limit, status=status)
    return {
        "runs": records,
        "total": len(records),
        "blocked": sum(1 for record in records if record.get("status") == "blocked"),
    }
