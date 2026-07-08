"""Cross-agent memory query intake (roadmap 5.3).

Polled by the Scheduler. Claims each pending memory query addressed to this
agent (atomically), recalls from its own memory via the live agent, and shares
the result in the originating chat. Claim marks the row done immediately, so
overlapping polls never double-answer.
"""

import asyncio
import logging

from kronos.config import settings
from kronos.cron.notify import send_webhook
from kronos.swarm_store import get_swarm

log = logging.getLogger("kronos.cron.memory_ask")

_MAX_PER_POLL = 5


async def _answer_memory(agent, req: dict) -> str | None:
    framing = (
        f"Коллега-агент ({req['from_agent']}) спрашивает, что у тебя есть в памяти "
        "по этому вопросу. Поделись только тем, что реально знаешь из своей памяти "
        "и опыта; если ничего нет — так и скажи коротко."
    )
    try:
        return await agent.ainvoke(
            message=req["query"],
            thread_id=req["thread_id"],
            user_id="memory-ask",
            session_id=str(req["chat_id"]),
            source_kind="peer_reaction",  # ephemeral — don't pollute my memory
            persist_user_turn=False,
            extra_system_context=framing,
        )
    except Exception as e:
        log.error("Memory request #%s failed: %s", req["id"], e)
        return None


async def run_memory_intake() -> None:
    from kronos.bridge import get_agent

    agent = get_agent()
    if agent is None:
        return

    swarm = get_swarm()
    processed = 0
    while processed < _MAX_PER_POLL:
        req = swarm.accept_next_memory_request(settings.agent_name)
        if req is None:
            break
        processed += 1

        answer = await _answer_memory(agent, req)
        ok = bool(answer) and await asyncio.to_thread(
            send_webhook, answer, req["chat_id"], None, req["topic_id"] or None
        )
        if ok:
            swarm.incr_metric("memory_requests_answered")
        else:
            swarm.complete_memory_request(req["id"], success=False)
            swarm.incr_metric("memory_requests_failed")
            log.warning("Memory request #%s not delivered", req["id"])
