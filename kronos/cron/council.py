"""Council intake — contribute positions and synthesize (roadmap 5.2).

Polled by the Scheduler. Each agent plays two roles against the shared ledger:
  1. participant — answer councils it's invited to, independently;
  2. initiator — once all positions are in, synthesize one answer for the chat.
Synthesis is claimed under an IMMEDIATE transaction so it fires exactly once.
"""

import asyncio
import logging

from kronos.config import settings
from kronos.cron.notify import send_webhook
from kronos.swarm_store import get_swarm

log = logging.getLogger("kronos.cron.council")

_MAX_PER_POLL = 3


async def _submit_position(agent, swarm, session: dict) -> None:
    framing = (
        "Идёт консилиум агентов. Дай СВОЮ независимую позицию по вопросу по своей "
        "специализации — кратко и по делу. Не синтезируй чужие мнения, только своя "
        "точка зрения."
    )
    try:
        position = await agent.ainvoke(
            message=session["question"],
            thread_id=session["thread_id"],
            user_id="council",
            session_id=str(session["chat_id"]),
            source_kind="peer_reaction",  # ephemeral — not the user's turn
            persist_user_turn=False,
            extra_system_context=framing,
        )
    except Exception as e:
        log.error("Council #%s position failed: %s", session["id"], e)
        return
    if position:
        swarm.submit_position(session["id"], settings.agent_name, position)


async def _synthesize(agent, swarm, session: dict) -> bool:
    positions = swarm.get_positions(session["id"])
    joined = "\n\n".join(f"[{p['agent_name']}]: {p['position']}" for p in positions)
    framing = (
        "Ты инициатор консилиума. Ниже независимые позиции коллег-агентов. "
        "Синтезируй ОДИН связный ответ пользователю: где согласие, где расхождения "
        "и твой итоговый вывод. Не описывай саму механику консилиума."
    )
    prompt = f"Вопрос: {session['question']}\n\nПозиции агентов:\n{joined}"
    try:
        answer = await agent.ainvoke(
            message=prompt,
            thread_id=session["thread_id"],
            user_id="council",
            session_id=str(session["chat_id"]),
            source_kind="user",
            persist_user_turn=True,
            extra_system_context=framing,
        )
    except Exception as e:
        log.error("Council #%s synthesis failed: %s", session["id"], e)
        return False
    return bool(answer) and await asyncio.to_thread(
        send_webhook, answer, session["chat_id"], None, session["topic_id"] or None
    )


async def run_council_intake() -> None:
    from kronos.bridge import get_agent

    agent = get_agent()
    if agent is None:
        return
    swarm = get_swarm()
    me = settings.agent_name

    # Role 1: contribute a position to councils this agent is invited to.
    for session in swarm.pending_council_tasks(me)[:_MAX_PER_POLL]:
        await _submit_position(agent, swarm, session)

    # Role 2: synthesize councils this agent initiated, once all positions are in.
    for session in swarm.councils_awaiting_synthesis(me)[:_MAX_PER_POLL]:
        claimed = swarm.claim_synthesis(session["id"], me)
        if claimed is None:
            continue  # not everyone has answered yet
        ok = await _synthesize(agent, swarm, claimed)
        swarm.complete_council(claimed["id"], success=ok)
        swarm.incr_metric("councils_completed" if ok else "councils_failed")
