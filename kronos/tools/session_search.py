"""Session search tool -- FTS5 search across all agent sessions."""

import logging
from datetime import datetime

from langchain_core.tools import tool

from kronos.swarm_store import get_swarm

log = logging.getLogger("kronos.tools.session_search")


@tool
def session_search(
    query: str, agent: str = "", days: int = 30, limit: int = 5,
) -> str:
    """Search across conversation history of all agents. Use to find what was discussed before.

    Args:
        query: Search keywords (Russian or English)
        agent: Optional agent name filter (e.g. 'kronos', 'nexus')
        days: How many days back to search (default 30)
        limit: Max results (default 5)
    """
    if not query.strip():
        return "Укажи поисковый запрос."

    swarm = get_swarm()
    results = swarm.search_sessions(
        query=query,
        agent_name=agent,
        days=days,
        limit=limit,
    )

    if not results:
        return f"Ничего не найдено по запросу '{query}' за последние {days} дней."

    lines = []
    for r in results:
        ts = datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M")
        snippet = r["content"][:200]
        lines.append(f"[{ts}] {r['agent_name']}/{r['role']}: {snippet}")

    return f"Найдено {len(results)} результатов:\n\n" + "\n---\n".join(lines)
