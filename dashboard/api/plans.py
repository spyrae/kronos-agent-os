"""Plans API — what the agent is still working on, days later.

A plan spends most of its life waiting, which makes it invisible: no log line,
no turn in flight, nothing on /health. The control room is where "still watching
that price, checked 41 times" becomes something a person can see — and where a
plan that parked itself waiting for the owner can be released.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from dashboard.auth import verify_token
from kronos import plan_conditions, plans
from kronos.config import settings

router = APIRouter(prefix="/api/plans", tags=["plans"], dependencies=[Depends(verify_token)])
log = logging.getLogger("kronos.dashboard.plans")


def _step_view(step: dict) -> dict:
    spec = plans.wait_spec(step)
    return {
        "id": step["id"],
        "seq": step["seq"],
        "title": step["title"],
        "prompt": step["prompt"],
        "state": step["state"],
        "depends_on": plans.dependency_ids(step),
        "waiting_for": plan_conditions.describe(spec) if spec else "",
        "wake_at": step["wake_at"],
        "checks": step["checks"],
        "attempts": step["attempts"],
        "notify": bool(step["notify"]),
        "result": step["result"],
        "updated_at": step["updated_at"],
    }


def _plan_view(plan: dict, *, with_steps: bool = True) -> dict:
    steps = plans.steps_of(plan["id"])
    view = {
        "id": plan["id"],
        "goal": plan["goal"],
        "state": plan["state"],
        "summary": plan["summary"],
        "created_at": plan["created_at"],
        "updated_at": plan["updated_at"],
        "expires_at": plan["expires_at"],
        "step_count": len(steps),
        "done_count": sum(1 for step in steps if step["state"] == plans.STEP_DONE),
        "failed_count": sum(1 for step in steps if step["state"] == plans.STEP_FAILED),
        "waiting_count": sum(1 for step in steps if step["state"] == plans.STEP_WAITING),
    }
    if with_steps:
        view["steps"] = [_step_view(step) for step in steps]
    return view


@router.get("")
async def list_plans(
    include_finished: bool = Query(False, alias="all"),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    rows = list(plans.list_plans(settings.agent_name, limit=limit))
    if include_finished:
        seen = {plan["id"] for plan in rows}
        for state in plans.PLAN_TERMINAL:
            rows += [p for p in plans.list_plans(settings.agent_name, state=state, limit=limit) if p["id"] not in seen]
    rows.sort(key=lambda plan: plan["id"], reverse=True)
    return {
        "plans": [_plan_view(plan) for plan in rows],
        "states": [plans.PLAN_ACTIVE, *plans.PLAN_TERMINAL],
    }


@router.get("/{plan_id}")
async def get_plan(plan_id: int) -> dict:
    plan = plans.get_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail=f"no plan #{plan_id}")
    return _plan_view(plan)


@router.post("/{plan_id}/resume")
async def resume_plan(plan_id: int, step: int = Query(0, description="one step, or 0 for every waiting step")) -> dict:
    """Release steps that were waiting for the owner. The poller takes it from there."""
    plan = plans.get_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail=f"no plan #{plan_id}")
    if plan["state"] != plans.PLAN_ACTIVE:
        raise HTTPException(status_code=409, detail=f"plan #{plan_id} is {plan['state']}")

    waiting = [s for s in plans.steps_of(plan_id) if s["state"] == plans.STEP_WAITING]
    if step:
        waiting = [s for s in waiting if s["id"] == step]
    if not waiting:
        raise HTTPException(status_code=404, detail="nothing is waiting on this plan")

    for parked in waiting:
        plans.release_step(parked["id"])
    log.info("Plan #%s: released %d step(s) from the dashboard", plan_id, len(waiting))
    return {"released": [s["id"] for s in waiting], "plan": _plan_view(plans.get_plan(plan_id))}


@router.delete("/{plan_id}")
async def cancel_plan(plan_id: int) -> dict:
    if not plans.cancel_plan(plan_id, settings.agent_name):
        raise HTTPException(status_code=404, detail=f"no active plan #{plan_id}")
    return _plan_view(plans.get_plan(plan_id))
