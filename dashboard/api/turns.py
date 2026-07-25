"""Durable turns API — see what is in flight, inspect it, resume or fork it.

Turn state used to be readable only by opening SQLite. Since a stuck turn means
"a process died and the user is still waiting", it belongs in the control room
next to health and the audit trail.

Resume and fork mutate state, so they are POSTs behind the same cookie auth as
everything else here.
"""

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from dashboard.auth import verify_token
from kronos.config import settings

router = APIRouter(prefix="/api/turns", tags=["turns"], dependencies=[Depends(verify_token)])
log = logging.getLogger("kronos.dashboard.turns")

IN_FLIGHT = {"running", "resuming"}


def _store():
    from kronos.session import SessionStore

    return SessionStore(settings.db_path, agent_name=settings.agent_name)


@router.get("")
async def list_turns(
    status: str = Query("", description="running|resuming|done|failed|superseded|recovered"),
    thread: str = Query(""),
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    """Recent durable turns plus the in-flight summary shown on /health."""
    store = _store()
    turns = await store.list_turns(status=status, thread_id=thread, limit=limit)
    stats = await store.running_turn_stats()

    counts: dict[str, int] = {}
    for turn in turns:
        key = str(turn.get("status") or "unknown")
        counts[key] = counts.get(key, 0) + 1

    return {"turns": turns, "counts": counts, "total": len(turns), **stats}


@router.get("/{turn_id}")
async def get_turn(turn_id: str) -> dict:
    """One turn: journal timeline, memoized tool results, recorded effects."""
    detail = await _store().get_turn_detail(turn_id)
    if not detail:
        raise HTTPException(status_code=404, detail=f"turn {turn_id} not found")
    return detail


@router.post("/{turn_id}/resume")
async def resume_turn(turn_id: str) -> dict:
    """Finish an interrupted turn now.

    Only in-flight turns can be resumed: re-running a finished turn would
    re-answer a question that was already answered.
    """
    store = _store()
    detail = await store.get_turn_detail(turn_id)
    if not detail:
        raise HTTPException(status_code=404, detail=f"turn {turn_id} not found")
    if detail["status"] not in IN_FLIGHT:
        raise HTTPException(status_code=409, detail=f"turn is '{detail['status']}', only in-flight turns can resume")

    from kronos.graph import KronosAgent

    agent = KronosAgent(session_store=store)
    answer = await agent.resume_interrupted_turn(
        {
            "turn_id": turn_id,
            "thread_id": detail["thread_id"],
            "input_message": detail.get("input_message", ""),
            "attempts": detail.get("attempts", 0),
        }
    )
    if not answer:
        raise HTTPException(status_code=500, detail="resume produced no answer; the turn was marked failed")
    return {"ok": True, "turn_id": turn_id, "answer": answer}


@router.post("/{turn_id}/fork")
async def fork_turn(turn_id: str, payload: dict = Body(default={})) -> dict:
    """Copy a turn's prefix into a new thread, leaving the original intact."""
    at_seq = int(payload.get("at_seq") or 0)
    thread = str(payload.get("thread") or "")

    result = await _store().fork_turn(turn_id, at_seq=at_seq, new_thread_id=thread)
    if not result:
        raise HTTPException(status_code=404, detail=f"turn {turn_id} not found")
    return {"ok": True, **result}
