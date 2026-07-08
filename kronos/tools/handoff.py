"""handoff_to_agent tool — route a request to a better-suited swarm agent (5.1).

The agent decides a request is outside its domain and hands it to the profile
agent via the shared swarm ledger; that agent's intake poll picks it up and
answers with its own expertise, instead of this agent going silent or replying
worse.
"""

from langchain_core.tools import tool

from kronos.audit import get_tool_audit_context
from kronos.config import settings
from kronos.group_router import AGENT_PROFILES
from kronos.swarm_store import get_swarm


@tool
def handoff_to_agent(to_agent: str, why: str) -> str:
    """Hand this request to a better-suited swarm agent when it's outside your domain.

    Use when the user's request clearly belongs to another agent's specialty —
    hand it off instead of answering worse yourself or staying silent. The
    target agent picks it up and replies here with its own expertise.

    Known agents and their domains:
    - kronos: strategic advisor — priorities, planning, multi-model analysis
    - nexus: data analyst — business metrics, SEO, competitive intelligence
    - lacuna: creative director — brand, product meaning, naming, creative vision
    - resonant: UX advocate — user experience, culture, tone of voice, onboarding
    - keystone: quality engineer — code architecture, standards, technical debt
    - impulse: action catalyst — momentum, quick wins, ideation

    Args:
        to_agent: the agent to hand off to (one of the names above).
        why: a self-contained brief for that agent — what the user needs plus any
            context, since they don't see this chat's framing.
    """
    me = settings.agent_name
    to_agent = to_agent.strip().lower()
    if to_agent not in AGENT_PROFILES:
        return f"Неизвестный агент '{to_agent}'. Доступны: {', '.join(sorted(AGENT_PROFILES))}."
    if to_agent == me:
        return "Нельзя передать запрос самому себе."

    ctx = get_tool_audit_context()
    chat_raw = ctx.get("session_id", "")
    thread_id = ctx.get("thread_id", "")
    if not chat_raw:
        return "Не удалось передать: неизвестен чат этого запроса."

    topic_id = None
    _, sep, tail = thread_id.partition(":")
    if sep and tail.isdigit():
        topic_id = int(tail)

    swarm = get_swarm()
    handoff_id = swarm.create_handoff(
        chat_id=int(chat_raw),
        topic_id=topic_id,
        thread_id=thread_id,
        from_agent=me,
        to_agent=to_agent,
        context=why,
    )
    swarm.incr_metric("handoffs_created")

    role = AGENT_PROFILES[to_agent].get("role", "")
    role_txt = f" ({role})" if role else ""
    return f"↪️ Передал запрос агенту {to_agent}{role_txt} — он ответит здесь. #{handoff_id}"
