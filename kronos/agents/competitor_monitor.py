"""Competitor Monitor Agent — on-demand competitive intelligence via supervisor.

Handles user queries about competitors:
- "как дела у конкурентов?" → status + recent changes
- "что нового у Wanderlog?" → specific competitor history + Mem0
- "проверь конкурентов" → live check
- "наши преимущества" → competitive advantage tracker
- "конкурентный анализ" → weekly report on demand
"""

import logging

from langchain_core.messages import BaseMessage, HumanMessage

from kronos.engine import AgentResult

log = logging.getLogger("kronos.agents.competitor_monitor")


def create_competitor_monitor_agent():
    """Create competitor monitor agent.

    Unlike other agents, this doesn't use MCP tools — it directly
    orchestrates the CompetitorMonitor pipeline + tracker + Mem0.
    """
    from kronos.competitors.config import load_competitors
    from kronos.competitors.digest import CompetitorMonitor
    from kronos.competitors.tracker import CompetitiveTracker

    monitor = CompetitorMonitor()
    tracker = CompetitiveTracker()
    competitors = load_competitors()
    comp_names = {c.name.lower(): c for c in competitors}

    async def run(messages: list[BaseMessage]) -> AgentResult:
        """Handle competitor-related queries."""
        user_msg = ""
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                user_msg = msg.content
                break

        if not user_msg:
            return AgentResult(messages=messages, content="No query provided.")

        user_lower = user_msg.lower()

        # Intent: live check
        if any(kw in user_lower for kw in ["check", "проверь", "обнови", "свежие", "fetch"]):
            digest = await monitor.run_daily_check()
            if digest is None:
                content = await monitor.get_status_summary()
                content += "\n\nНет новых изменений."
            else:
                content = digest

        # Intent: competitive advantages / our position
        elif any(kw in user_lower for kw in [
            "преимущ", "advantage", "позици", "position", "tracker", "трекер",
        ]):
            content = tracker.format_summary()

        # Intent: dashboard
        elif any(kw in user_lower for kw in [
            "dashboard", "дашборд", "таблиц", "рейтинг", "ratings",
        ]):
            from kronos.competitors.dashboard import generate_dashboard_markdown
            content = generate_dashboard_markdown()

        # Intent: weekly report on demand
        elif any(kw in user_lower for kw in [
            "анализ", "analysis", "report", "отчёт", "отчет", "weekly",
        ]):
            from kronos.competitors.weekly_report import generate_weekly_report
            report, _ = await generate_weekly_report()
            content = report

        # Intent: specific competitor query
        elif _find_competitor(user_lower, comp_names):
            comp = _find_competitor(user_lower, comp_names)
            content = await _competitor_deep_query(comp.name, user_msg, monitor.store)

        # Intent: default — status + recent changes
        else:
            content = await monitor.get_status_summary()

        return AgentResult(messages=messages, content=content)

    run.__name__ = "competitor_monitor_agent"
    run.__qualname__ = "competitor_monitor_agent"
    return run


def _find_competitor(text: str, comp_names: dict):
    """Find a specific competitor mentioned in the query."""
    for name, comp in comp_names.items():
        if name in text or comp.id in text:
            return comp
    return None


async def _competitor_deep_query(
    comp_name: str,
    query: str,
    store,
) -> str:
    """Answer a question about a specific competitor using DB + Mem0."""
    # Get recent changes from DB
    changes = store._db.read(
        "SELECT * FROM competitor_changes WHERE competitor_id = ? "
        "ORDER BY detected_at DESC LIMIT 20",
        (comp_name.lower().replace(" ", "_"),),
    )

    lines = [f"Recent activity for {comp_name}:"]
    if changes:
        for ch in [dict(r) for r in changes]:
            lines.append(f"  • [{ch['severity']}] {ch['summary']}")
    else:
        lines.append("  No recorded changes yet.")

    # Add Mem0 context
    try:
        from kronos.memory.store import search_memories
        memories = search_memories(
            f"{comp_name} competitor activity",
            user_id="competitor_monitor",
            limit=3,
        )
        if memories:
            lines.append(f"\nFrom memory:")
            for m in memories:
                lines.append(f"  • {m}")
    except Exception:
        pass

    return "\n".join(lines)
