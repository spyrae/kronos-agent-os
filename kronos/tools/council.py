"""convene_council tool — structured multi-agent debate (roadmap 5.2).

The initiator convenes 2-3 agents who each answer the question independently via
the shared ledger; once all positions are in, the initiator's intake poll
synthesizes them into one answer for the chat.
"""

from langchain_core.tools import tool

from kronos.audit import get_tool_audit_context
from kronos.config import settings
from kronos.group_router import AGENT_PROFILES
from kronos.swarm_store import get_swarm


@tool
def convene_council(question: str, participants: list[str]) -> str:
    """Convene a council: gather independent positions from 2-3 agents, then synthesize.

    Use for a hard or cross-domain question, or when the user asks to "discuss" /
    "обсудите". Each named agent answers the question independently with its own
    expertise; once all have, you synthesize one combined answer for the chat.

    Known agents and domains:
    - kronos: strategic advisor — priorities, planning, multi-model analysis
    - nexus: data analyst — metrics, SEO, competitive intelligence
    - lacuna: creative director — brand, meaning, naming, creative vision
    - resonant: UX advocate — user experience, culture, tone of voice
    - keystone: quality engineer — architecture, standards, technical debt
    - impulse: action catalyst — momentum, quick wins, ideation
    Pick 2-3 whose domains fit; don't include yourself.

    Args:
        question: the question each participant answers independently — make it
            self-contained (they don't see this chat).
        participants: 2-3 agent names to convene.
    """
    me = settings.agent_name

    if isinstance(participants, str):
        participants = participants.replace(" ", ",").split(",")
    names = [p.strip().lower() for p in participants if p and p.strip()]
    names = [p for p in dict.fromkeys(names) if p != me]  # dedup, drop self

    unknown = [p for p in names if p not in AGENT_PROFILES]
    if unknown:
        return f"Неизвестные агенты: {', '.join(unknown)}. Доступны: {', '.join(sorted(AGENT_PROFILES))}."
    if len(names) < 2:
        return "Для консилиума нужно минимум 2 участника (кроме тебя)."
    if len(names) > 4:
        return "Слишком много участников — максимум 4."

    ctx = get_tool_audit_context()
    chat_raw = ctx.get("session_id", "")
    thread_id = ctx.get("thread_id", "")
    if not chat_raw:
        return "Не удалось созвать консилиум: неизвестен чат этого запроса."

    topic_id = None
    _, sep, tail = thread_id.partition(":")
    if sep and tail.isdigit():
        topic_id = int(tail)

    swarm = get_swarm()
    session_id = swarm.create_council(
        chat_id=int(chat_raw),
        topic_id=topic_id,
        thread_id=thread_id,
        initiator=me,
        question=question,
        participants=names,
    )
    swarm.incr_metric("councils_convened")
    return (
        f"🏛 Созвал консилиум #{session_id}: {', '.join(names)}. "
        "Соберу их позиции и синтезирую единый ответ здесь."
    )
