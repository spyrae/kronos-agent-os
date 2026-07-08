"""ask_agent_memory tool — query another agent's private memory (roadmap 5.3).

Each agent keeps its own Mem0/FTS, so a colleague may know things this agent
doesn't. The target agent looks in its memory and shares relevant knowledge in
the chat — making the swarm smarter than any single agent.
"""

from langchain_core.tools import tool

from kronos.audit import get_tool_audit_context
from kronos.config import settings
from kronos.group_router import AGENT_PROFILES
from kronos.swarm_store import get_swarm


@tool
def ask_agent_memory(to_agent: str, query: str) -> str:
    """Ask another agent what it knows about something from its own memory.

    Use when a colleague's private memory likely holds context you lack. The
    target agent recalls from its memory and shares here in the chat.

    Known agents: kronos, nexus, lacuna, resonant, keystone, impulse.

    Args:
        to_agent: the agent to ask.
        query: what you want them to recall (e.g. "что мы решили про нейминг X").
    """
    me = settings.agent_name
    to_agent = to_agent.strip().lower()
    if to_agent not in AGENT_PROFILES:
        return f"Неизвестный агент '{to_agent}'. Доступны: {', '.join(sorted(AGENT_PROFILES))}."
    if to_agent == me:
        return "Свою память спрашивать не нужно — она у тебя уже под рукой."

    ctx = get_tool_audit_context()
    chat_raw = ctx.get("session_id", "")
    thread_id = ctx.get("thread_id", "")
    if not chat_raw:
        return "Не удалось спросить: неизвестен чат этого запроса."

    topic_id = None
    _, sep, tail = thread_id.partition(":")
    if sep and tail.isdigit():
        topic_id = int(tail)

    swarm = get_swarm()
    request_id = swarm.create_memory_request(
        chat_id=int(chat_raw),
        topic_id=topic_id,
        thread_id=thread_id,
        from_agent=me,
        to_agent=to_agent,
        query=query,
    )
    swarm.incr_metric("memory_requests_created")
    return f"🧠 Спросил {to_agent}, что у него есть про это — он поделится здесь. #{request_id}"
