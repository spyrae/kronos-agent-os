"""Weekly Market Review — automated investment brief.

Pipeline: Yahoo Finance (prices) + Brave Search (news) → LLM synthesis → Telegram.
Runs weekly Friday at 18:00 UTC+8 (10:00 UTC).
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

from kronos.config import settings
from kronos.cron.notify import send_bot_api, TOPIC_DIGEST
from kronos.llm import ModelTier, get_model
from kronos.tools.brave import search as brave_search

log = logging.getLogger("kronos.cron.market_review")


def _load_watchlist() -> list[str]:
    """Load tickers from investment-analysis watchlist."""
    from kronos.workspace import ws
    path = ws.skill_ref("investment-analysis", "WATCHLIST")
    if not path.exists():
        return ["AAPL", "GOOGL", "MSFT", "NVDA", "TSLA"]

    tickers = []
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        # Match lines like "| AAPL |" or "- AAPL"
        if line.startswith("| "):
            parts = [p.strip() for p in line.split("|") if p.strip()]
            if parts and parts[0].isupper() and 2 <= len(parts[0]) <= 5:
                tickers.append(parts[0])
        elif line.startswith("- ") and line[2:].strip().isupper():
            ticker = line[2:].strip().split()[0]
            if 2 <= len(ticker) <= 5:
                tickers.append(ticker)

    return tickers or ["AAPL", "GOOGL", "MSFT", "NVDA", "TSLA"]


async def run_market_review() -> None:
    """Generate weekly market review. Kronos only."""
    if settings.agent_name != "kronos":
        return

    tickers = _load_watchlist()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Collect news for each ticker
    all_data = []
    for ticker in tickers[:10]:
        results = brave_search(f"{ticker} stock news this week", count=3, freshness="pw")
        news = "\n".join(f"  - {r.title}: {r.description}" for r in results[:3])
        all_data.append(f"**{ticker}**:\n{news}")

    if not all_data:
        log.info("No market data collected, skipping review")
        return

    market_text = "\n\n".join(all_data)

    prompt = f"""Ты — инвестиционный аналитик. Дата: {today}.

Новости по watchlist за неделю:
{market_text[:5000]}

Создай еженедельный обзор:
1. Общий обзор рынка (2-3 предложения)
2. По каждому тикеру: ключевое событие недели + sentiment (🟢🟡🔴)
3. На что обратить внимание на следующей неделе
4. Actionable: купить/продать/держать (если есть сильные сигналы)

Формат: HTML (<b>, <i>). Русский. Краткий, до 1500 символов.
"""

    model = get_model(ModelTier.STANDARD)
    from langchain_core.messages import HumanMessage
    response = model.invoke([HumanMessage(content=prompt)])
    review = response.content if isinstance(response.content, str) else str(response.content)

    if not review or len(review) < 50:
        return

    log.info("Market review: %d chars, %d tickers", len(review), len(tickers))
    send_bot_api(f"<b>📈 Weekly Market Review — {today}</b>\n\n{review}", parse_mode="HTML", topic_id=TOPIC_DIGEST)
