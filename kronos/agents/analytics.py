"""Analytics Agent — on-demand infra/product/business queries via supervisor.

Handles:
- "серверы живы?" → current health status
- "сколько пользователей?" → PostHog/Supabase data
- "MRR?" → RevenueCat data
- "daily pulse" → full health digest
- "weekly report" → full business report
- "аномалии?" → current anomaly check
- "тренды?" → 4-week trend analysis
"""

import logging

from langchain_core.messages import BaseMessage, HumanMessage

from kronos.engine import AgentResult

log = logging.getLogger("kronos.agents.analytics")

_PULSE_KEYWORDS = [
    "pulse", "пульс", "здоровье", "health", "обзор",
    "как дела", "как у нас", "статус", "status", "дайджест",
    "как продукт",
]

_WEEKLY_KEYWORDS = [
    "weekly", "недельный", "за неделю", "бизнес-отчёт", "бизнес отчёт",
    "weekly report", "полный отчёт",
]

_ANOMALY_KEYWORDS = [
    "аномали", "anomal", "отклонен", "deviation", "что не так",
]

_TREND_KEYWORDS = [
    "тренд", "trend", "динамик", "рост", "падение", "wow",
]


def create_analytics_agent():
    """Create analytics agent for supervisor delegation."""

    async def run(messages: list[BaseMessage]) -> AgentResult:
        """Handle infra/analytics/product/business queries."""
        user_msg = ""
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                user_msg = msg.content
                break

        if not user_msg:
            return AgentResult(messages=messages, content="No query provided.")

        user_lower = user_msg.lower()

        # Intent: weekly business report
        if any(kw in user_lower for kw in _WEEKLY_KEYWORDS):
            from kronos.analytics.weekly_report import generate_weekly_report
            report, _ = await generate_weekly_report()
            content = report

        # Intent: anomaly check
        elif any(kw in user_lower for kw in _ANOMALY_KEYWORDS):
            from kronos.analytics.pulse import _collect_all
            from kronos.analytics.anomaly import check_all_anomalies, flatten_metrics

            metrics = _collect_all()
            flat = flatten_metrics(metrics)
            anomalies = check_all_anomalies(flat)

            if anomalies:
                content = "\U0001f6a8 Обнаружены аномалии:\n\n"
                for a in anomalies:
                    content += a.format_alert() + "\n"
            else:
                content = "\u2705 Аномалий не обнаружено. Все метрики в пределах нормы."

        # Intent: trend analysis
        elif any(kw in user_lower for kw in _TREND_KEYWORDS):
            from kronos.analytics.trends import analyze_trends, format_trends_summary

            trends = analyze_trends()
            if trends:
                content = "\U0001f4c8 Тренды за последние 2 недели:\n\n"
                for t in trends:
                    content += t.format_line() + "\n"
            else:
                content = "Недостаточно данных для анализа трендов (нужно мини��ум 2 недели)."

        # Intent: daily pulse
        elif any(kw in user_lower for kw in _PULSE_KEYWORDS):
            from kronos.analytics.pulse import generate_daily_pulse
            pulse, _ = await generate_daily_pulse()
            content = pulse

        # Intent: specific question
        else:
            from kronos.analytics.pulse import answer_health_query
            content = await answer_health_query(user_msg)

        return AgentResult(messages=messages, content=content)

    run.__name__ = "analytics_agent"
    run.__qualname__ = "analytics_agent"
    return run
