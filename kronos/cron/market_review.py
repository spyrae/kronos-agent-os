"""Weekly Market Review — automated investment brief.

Pipeline: watchlist + Brave/Exa news search → LLM synthesis → Telegram.
Runs weekly Friday at 18:00 UTC+8 (10:00 UTC).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kronos.config import settings
from kronos.cron.notify import TOPIC_FINANCE, send_bot_api
from kronos.llm import ModelTier, get_model
from kronos.tools.brave import search as brave_search
from kronos.workspace import Workspace

log = logging.getLogger("kronos.cron.market_review")

DEFAULT_TICKERS = ("AAPL", "GOOGL", "MSFT", "NVDA", "TSLA")
MAX_TICKERS = 10
NEWS_PER_TICKER = 3


@dataclass(frozen=True)
class TickerNews:
    """Small source-backed news bundle for one ticker."""

    ticker: str
    items: tuple[tuple[str, str], ...]


def _load_watchlist(workspace: Workspace | None = None) -> list[str]:
    """Load tickers from investment-analysis watchlist."""
    active_workspace = workspace
    if active_workspace is None:
        from kronos.workspace import ws

        active_workspace = ws

    path = active_workspace.skill_ref("investment-analysis", "WATCHLIST")
    if not path.exists():
        return list(DEFAULT_TICKERS)

    tickers = []
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        ticker = _ticker_from_watchlist_line(line)
        if ticker and ticker not in tickers:
            tickers.append(ticker)

    return tickers or list(DEFAULT_TICKERS)


def collect_market_news(
    tickers: Sequence[str],
    *,
    search_fn=brave_search,
    max_tickers: int = MAX_TICKERS,
    news_per_ticker: int = NEWS_PER_TICKER,
) -> list[TickerNews]:
    """Collect source-backed weekly news snippets for a ticker watchlist."""
    bundles: list[TickerNews] = []
    for ticker in tickers[:max_tickers]:
        clean_ticker = _normalize_ticker(ticker)
        if not clean_ticker:
            continue
        try:
            results = search_fn(
                f"{clean_ticker} stock news this week",
                count=news_per_ticker,
                freshness="pw",
            )
        except Exception as exc:
            log.warning("Market review search failed for %s: %s", clean_ticker, exc)
            continue

        items = tuple(
            (_compact(str(getattr(result, "title", "") or "")), _compact(str(getattr(result, "description", "") or "")))
            for result in list(results)[:news_per_ticker]
            if getattr(result, "title", "") or getattr(result, "description", "")
        )
        if items:
            bundles.append(TickerNews(clean_ticker, items))
    return bundles


def build_market_review_prompt(news: Sequence[TickerNews], *, today: str) -> str:
    """Build the LLM prompt for a non-advisory weekly market brief."""
    market_text = "\n\n".join(_format_ticker_news(bundle) for bundle in news)
    return f"""Ты — аналитик рынков. Дата: {today}.

Новости по watchlist за неделю:
{market_text[:5000]}

Создай еженедельный обзор портфельного watchlist:
1. Общий обзор рынка (2-3 предложения).
2. По каждому тикеру: ключевое событие недели + sentiment (🟢🟡🔴).
3. На что обратить внимание на следующей неделе.
4. Watchlist actions: monitor / review thesis / reduce risk / wait for data.

Safety:
- Это не индивидуальная инвестиционная рекомендация.
- Не пиши прямые команды купить/продать.
- Отделяй факты из новостей от интерпретации.

Формат: HTML (<b>, <i>). Русский. Краткий, до 1500 символов.
"""


async def run_market_review(
    *,
    tickers: Sequence[str] | None = None,
    search_fn=brave_search,
    model_factory: Callable[[ModelTier], Any] = get_model,
    sender: Callable[..., bool] = send_bot_api,
    now: datetime | None = None,
    workspace: Workspace | None = None,
) -> bool:
    """Generate weekly market review. Kronos only."""
    if settings.agent_name != "kronos":
        return False

    active_tickers = list(tickers) if tickers is not None else _load_watchlist(workspace)
    current_time = now or datetime.now(UTC)
    today = current_time.strftime("%Y-%m-%d")
    news = collect_market_news(active_tickers, search_fn=search_fn)

    if not news:
        log.info("No market data collected, skipping review")
        return False

    prompt = build_market_review_prompt(news, today=today)
    model = model_factory(ModelTier.STANDARD)
    from langchain_core.messages import HumanMessage

    response = model.invoke([HumanMessage(content=prompt)])
    review = response.content if isinstance(response.content, str) else str(response.content)

    if not review or len(review) < 50:
        return False

    log.info("Market review: %d chars, %d tickers", len(review), len(active_tickers))
    return bool(
        sender(
            f"<b>📈 Weekly Market Review — {today}</b>\n\n{review}",
            parse_mode="HTML",
            topic_id=TOPIC_FINANCE,
        )
    )


def _format_ticker_news(bundle: TickerNews) -> str:
    news = "\n".join(f"  - {title}: {description}" for title, description in bundle.items)
    return f"**{bundle.ticker}**:\n{news}"


def _ticker_from_watchlist_line(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("| "):
        parts = [part.strip() for part in stripped.split("|") if part.strip()]
        return _normalize_ticker(parts[0]) if parts else ""
    if stripped.startswith("- "):
        return _normalize_ticker(stripped[2:].strip().split()[0])
    return ""


def _normalize_ticker(value: str) -> str:
    ticker = "".join(ch for ch in str(value or "").upper() if ch.isalnum() or ch in {".", "-"})
    if ticker in {"TICKER", "SYMBOL"}:
        return ""
    if 1 <= len(ticker) <= 8 and any(ch.isalpha() for ch in ticker):
        return ticker
    return ""


def _compact(text: str) -> str:
    return " ".join((text or "").split())
