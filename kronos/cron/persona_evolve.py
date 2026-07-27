"""Weekly persona evolution — propose a persona edit from feedback (roadmap 6.3).

Reads the week's satisfaction + negative feedback, asks the LLM for ONE concrete
edit to SOUL or IDENTITY, stores it as a pending proposal, and notifies the user
in Telegram to approve/reject via /persona. Nothing is applied without approval.

Since moat 12.4 every proposal is measured before the owner sees it (see
`kronos.evolution_eval`): the patched persona is applied to a copy of the
workspace, the offline scenario suite is run against both, and a proposal that
breaks prompt assembly, regresses a scenario, or merely repeats what the file
already says is auto-rejected without a notification. What that measurement can
and cannot prove is documented in that module — it cannot score answer quality,
and the report says so rather than implying otherwise.
"""

import json
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

    # Measure before anyone is asked (moat 12.4). A proposal that breaks prompt
    # assembly, regresses a scenario or merely repeats what the file already says
    # is decided here; the owner's attention is for judgement calls.
    from kronos.evolution_eval import measure_proposal, render_report, verdict
    from kronos.policy import get_policy

    candidate = {"target": target, "rationale": rationale, "proposal": proposal}
    try:
        measurement = await measure_proposal(candidate)
    except Exception as e:
        log.warning("Persona proposal could not be measured: %s", e)
        measurement = {"measured_quality": False, "notes": [f"measurement failed: {e}"], "regressions": []}

    evolution_policy = get_policy().evolution
    acceptable, reason = verdict(measurement, max_regression_pct=evolution_policy.max_regression_pct)
    measurement["verdict"] = reason

    pid = evolution.create_proposal(
        agent_name=agent,
        target=target,
        rationale=rationale,
        proposal=proposal,
        eval_json=json.dumps(measurement, ensure_ascii=False),
    )
    swarm.incr_metric("persona_proposals_created")

    if not acceptable and evolution_policy.auto_reject:
        evolution.decide_proposal(pid, agent, approved=False)
        evolution.record_decision_reason(pid, reason)
        swarm.incr_metric("persona_proposals_auto_rejected")
        # No push: a proposal the measurement refuted is noise, not news. It stays
        # visible in `/persona list --rejected` with its reason.
        log.info("Persona proposal #%s auto-rejected: %s", pid, reason)
        return

    send_bot_api(
        f"🧬 Предложение эволюции персоны #{pid} → {target.upper()}\n\n"
        f"Почему: {rationale}\n\n"
        f"Изменение:\n{proposal}\n\n"
        f"Замер:\n{render_report(measurement)}\n\n"
        f"Применить: /persona approve {pid} · Отклонить: /persona reject {pid}",
        topic_id=TOPIC_GENERAL,
    )
    log.info("Persona proposal #%s created for %s", pid, target)
