"""Weekly persona evolution — propose a persona edit from feedback (roadmap 6.3).

Reads the week's satisfaction + negative feedback, asks the LLM for ONE concrete
edit to SOUL or IDENTITY, stores it as a pending proposal, and notifies the user
in Telegram to approve/reject via /persona. Nothing is applied without approval.
"""

import logging

from langchain_core.messages import HumanMessage

from kronos import evolution
from kronos.config import settings
from kronos.cron.notify import TOPIC_GENERAL, send_bot_api
from kronos.llm import ModelTier, get_model
from kronos.swarm_store import get_swarm

log = logging.getLogger("kronos.cron.persona_evolve")

_MIN_FEEDBACK = 3  # need at least some signal before proposing


def _parse_proposal(reply: str):
    """Parse the LLM's TARGET/RATIONALE/PROPOSAL block. Returns tuple or None."""
    text = reply.strip()
    if text.upper().startswith("SKIP"):
        return None
    target = None
    rationale = ""
    proposal_lines: list[str] = []
    mode = None
    for line in text.splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith("TARGET:"):
            val = stripped.split(":", 1)[1].strip().lower()
            target = val if val in evolution.VALID_TARGETS else None
            mode = None
        elif upper.startswith("RATIONALE:"):
            rationale = stripped.split(":", 1)[1].strip()
            mode = None
        elif upper.startswith("PROPOSAL:"):
            first = stripped.split(":", 1)[1].strip()
            if first:
                proposal_lines.append(first)
            mode = "proposal"
        elif mode == "proposal":
            proposal_lines.append(line)
    proposal = "\n".join(proposal_lines).strip()
    if not target or not rationale or not proposal:
        return None
    return target, rationale, proposal


async def run_persona_evolution() -> None:
    swarm = get_swarm()
    agent = settings.agent_name

    satisfaction = swarm.get_satisfaction_rate(agent_name=agent, days=7)
    total = satisfaction.get("total", 0)
    if total < _MIN_FEEDBACK:
        log.info("Persona evolution: only %d feedback signals, skipping", total)
        return

    positive = satisfaction.get("positive", 0)
    rate = positive / total if total else 0
    negative = swarm.get_feedback(agent_name=agent, reaction="negative", days=7, limit=15)
    neg_text = "\n".join(f"- {f.get('emoji', '')} on msg {f.get('msg_id')}" for f in negative) or "нет явного негатива"

    prompt = f"""Ты — агент {agent}. На основе фидбека за неделю предложи ОДНО конкретное
изменение к своей персоне (SOUL или IDENTITY), которое улучшит реакцию пользователя.

Satisfaction: {rate:.0%} (положительных {positive} из {total}).
Негативные реакции:
{neg_text}

Ответь СТРОГО в формате:
TARGET: soul|identity
RATIONALE: <одно предложение — почему это изменение>
PROPOSAL: <конкретный текст, готовый к вставке в файл — 2-5 строк>

Если менять нечего — ответь одним словом: SKIP."""

    model = get_model(ModelTier.STANDARD)
    response = model.invoke([HumanMessage(content=prompt)])
    reply = response.content if isinstance(response.content, str) else str(response.content)

    parsed = _parse_proposal(reply)
    if parsed is None:
        log.info("Persona evolution: nothing to propose")
        return

    target, rationale, proposal = parsed
    pid = evolution.create_proposal(agent_name=agent, target=target, rationale=rationale, proposal=proposal)
    swarm.incr_metric("persona_proposals_created")

    send_bot_api(
        f"🧬 Предложение эволюции персоны #{pid} → {target.upper()}\n\n"
        f"Почему: {rationale}\n\n"
        f"Изменение:\n{proposal}\n\n"
        f"Применить: /persona approve {pid} · Отклонить: /persona reject {pid}",
        topic_id=TOPIC_GENERAL,
    )
    log.info("Persona proposal #%s created for %s", pid, target)
