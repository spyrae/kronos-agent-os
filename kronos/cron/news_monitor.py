"""News Monitor — daily digest from watchlist sources.

Pipeline: Brave Search (real data) → LLM (synthesis) → Telegram.
"""

import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from kronos.config import settings
from kronos.cron.notify import send_bot_api, TOPIC_DIGEST
from kronos.llm import ModelTier, get_model
from kronos.tools.brave import search as brave_search

log = logging.getLogger("kronos.cron.news_monitor")


def _load_watchlist() -> str:
    """Load watchlist from workspace."""
    from kronos.workspace import ws
    path = ws.skill_ref("news-monitor", "WATCHLIST")
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def _parse_watchlist_queries(watchlist: str) -> list[str]:
    """Extract search queries from watchlist markdown tables.

    Parses Reddit subreddits and Twitter accounts, generates
    targeted Brave Search queries for each.
    """
    import re

    queries = []

    # Parse Reddit table rows: | r/subreddit | description | filter |
    for match in re.finditer(r"\|\s*(r/\w+)\s*\|([^|]+)\|", watchlist):
        subreddit = match.group(1).strip()
        description = match.group(2).strip()
        queries.append(f"site:reddit.com {subreddit} {description}")

    # Parse Twitter table rows: | @account | description | topic |
    for match in re.finditer(r"\|\s*(@\w+)\s*\|([^|]+)\|([^|]+)\|", watchlist):
        account = match.group(1).strip()
        description = match.group(2).strip()
        queries.append(f"{description} {account} news")

    # Fallback if nothing parsed
    if not queries:
        queries = ["AI news today", "tech news today", "LLM news today"]

    return queries


async def run_news_monitor() -> None:
    """Generate daily news digest with real search data. Kronos only."""
    if settings.agent_name != "kronos":
        return

    watchlist = _load_watchlist()
    if not watchlist:
        log.info("No watchlist found, skipping news-monitor")
        return

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    topics = _parse_watchlist_queries(watchlist)

    # Phase 1: Collect real search results
    all_results = []
    for topic in topics[:10]:  # cap at 10 topics
        results = brave_search(topic, count=5, freshness="pd")
        for r in results:
            all_results.append(f"[{topic}] {r.title} — {r.description}\n  {r.url}")

    if not all_results:
        log.warning("No search results for any topic, skipping digest")
        return

    search_data = "\n\n".join(all_results[:50])  # cap total results
    log.info("Collected %d search results from %d topics", len(all_results), len(topics))

    # Phase 2: LLM synthesis
    prompt = f"""Ты — News Monitor агент. Дата: {yesterday}.

Вот реальные результаты поиска за вчера:

{search_data}

Задача: составь дайджест из этих результатов.

Правила:
- Используй ТОЛЬКО данные из результатов поиска выше
- Группируй по темам
- Для каждой новости: заголовок, суть в 1-2 предложениях, ссылка
- Формат: HTML (используй <b>, <i>, <a href>)
- Максимум 10-15 пунктов, выбирай самые значимые
- Фильтруй мусор, повторы, рекламу
- Если по теме нет значимых новостей — не упоминай
- Русский язык

Формат:
<b>📰 Дайджест {yesterday}</b>

<b>🤖 AI/ML</b>
• <b>Заголовок</b> — суть (<a href="url">source</a>)
..."""

    model = get_model(ModelTier.LITE)
    from langchain_core.messages import HumanMessage
    response = model.invoke([HumanMessage(content=prompt)])
    digest = response.content if isinstance(response.content, str) else str(response.content)

    if not digest or len(digest) < 100:
        log.warning("Empty digest after synthesis, skipping")
        return

    log.info("News digest generated: %d chars", len(digest))
    send_bot_api(digest, parse_mode="HTML", topic_id=TOPIC_DIGEST)
