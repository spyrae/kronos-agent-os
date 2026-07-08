"""Cross-agent hand-off intake (roadmap 5.1).

Polled by the Scheduler. Atomically claims each pending hand-off addressed to
this agent, runs it through the local agent (its own expertise), and delivers
the result to the originating chat. Rows are claimed one at a time under an
IMMEDIATE transaction, so overlapping polls never double-process.
"""

import asyncio
import logging

from kronos.config import settings
from kronos.cron.notify import send_webhook
from kronos.swarm_store import get_swarm

log = logging.getLogger("kronos.cron.handoff")

# Safety bound per poll so a flood of hand-offs can't monopolize a cycle.
_MAX_PER_POLL = 5


async def _run_handoff(agent, handoff: dict) -> str | None:
    """Answer a handed-off request with this agent's expertise."""
    framing = (
        f"Другой агент ({handoff['from_agent']}) передал тебе этот запрос как "
        "профильному. Ответь по своей специализации; не упоминай саму механику "
        "передачи."
    )
    try:
        return await agent.ainvoke(
            message=handoff["context"],
            thread_id=handoff["thread_id"],
            user_id="handoff",
            session_id=str(handoff["chat_id"]),
            source_kind="user",
            persist_user_turn=True,
            extra_system_context=framing,
        )
    except Exception as e:
        log.error("Hand-off #%s agent run failed: %s", handoff["id"], e)
        return None


async def run_handoff_intake() -> None:
    from kronos.bridge import get_agent

    agent = get_agent()
    if agent is None:
        return  # not ready — leave pending hand-offs for the next poll

    swarm = get_swarm()
    processed = 0
    while processed < _MAX_PER_POLL:
        handoff = swarm.accept_next_handoff(settings.agent_name)
        if handoff is None:
            break
        processed += 1

        reply = await _run_handoff(agent, handoff)
        ok = bool(reply) and await asyncio.to_thread(
            send_webhook, reply, handoff["chat_id"], None, handoff["topic_id"] or None
        )
        swarm.complete_handoff(handoff["id"], success=ok)
        swarm.incr_metric("handoffs_completed" if ok else "handoffs_failed")
        if not ok:
            log.warning("Hand-off #%s not delivered", handoff["id"])
