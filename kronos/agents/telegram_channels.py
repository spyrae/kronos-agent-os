"""Telegram Channels Agent — public channel monitoring, digests, analytics.

Parses public Telegram channels via t.me/s/ web preview.
No API keys, no MTProto, zero config.

Uses native async tools (not MCP), so tools are created directly
rather than filtered from the MCP tool list.
"""

import logging

from langchain_core.tools import StructuredTool

from kronos.engine import create_agent
from kronos.llm import ModelTier, get_model
from kronos.tools.telegram_channels import (
    compare_channels,
    digest,
    fetch_posts,
    get_channel_info,
    search_posts,
    top_posts,
)

log = logging.getLogger("kronos.agents.telegram_channels")


# ---------------------------------------------------------------------------
# LangChain tool wrappers (sync wrappers over async functions)
# ---------------------------------------------------------------------------

def _format_posts(posts, max_text: int = 200) -> str:
    """Format posts list as compact text for LLM."""
    if not posts:
        return "Постов не найдено."
    lines = []
    for p in posts:
        text = p.text[:max_text] + "..." if len(p.text) > max_text else p.text
        fwd = f" (fwd: {p.fwd_from})" if p.fwd_from else ""
        reactions = f" | {p.reactions} reactions" if p.reactions else ""
        lines.append(
            f"[{p.date}] {p.views} views{reactions}{fwd}\n{text}"
        )
    return "\n\n".join(lines)


async def _tool_fetch_posts(
    channel: str,
    limit: int = 20,
    after_date: str = "",
) -> str:
    """Получить последние посты из публичного Telegram-канала.

    Args:
        channel: Username канала (любой формат: @username, t.me/username, просто username).
        limit: Максимум постов (по умолчанию 20, макс 100).
        after_date: Только посты начиная с этой даты (YYYY-MM-DD), опционально.
    """
    posts = await fetch_posts(
        channel,
        limit=min(limit, 100),
        after_date=after_date or None,
    )
    return _format_posts(posts)


async def _tool_channel_info(channel: str) -> str:
    """Получить информацию о публичном Telegram-канале: название, описание, подписчики.

    Args:
        channel: Username канала.
    """
    info = await get_channel_info(channel)
    return (
        f"Канал: @{info.username}\n"
        f"Название: {info.title}\n"
        f"Описание: {info.description}\n"
        f"Подписчики: {info.subscribers}"
    )


async def _tool_search_posts(channel: str, query: str, limit: int = 50) -> str:
    """Поиск по постам публичного Telegram-канала.

    Args:
        channel: Username канала.
        query: Поисковый запрос (ищет по тексту постов).
        limit: Сколько постов загрузить для поиска (по умолчанию 50).
    """
    posts = await search_posts(channel, query, limit=min(limit, 100))
    return _format_posts(posts)


async def _tool_top_posts(
    channel: str,
    sort_by: str = "views",
    limit: int = 50,
    top_n: int = 10,
) -> str:
    """Топ постов канала по просмотрам или реакциям (шер-парад).

    Args:
        channel: Username канала.
        sort_by: Сортировка — 'views' или 'reactions'.
        limit: Сколько постов загрузить (по умолчанию 50).
        top_n: Сколько топовых показать (по умолчанию 10).
    """
    posts = await top_posts(channel, limit=min(limit, 100), sort_by=sort_by, top_n=top_n)
    return _format_posts(posts)


async def _tool_digest(
    channels: str,
    period: str = "today",
) -> str:
    """Дайджест постов из нескольких Telegram-каналов за период.

    Args:
        channels: Каналы через запятую (например: 'channel1,channel2,channel3').
        period: Период — 'today', 'yesterday', 'week', или число дней (например '3').
    """
    ch_list = [c.strip() for c in channels.split(",") if c.strip()]
    if not ch_list:
        return "Ошибка: укажите хотя бы один канал."

    entries = await digest(ch_list, period=period)

    parts = []
    total = 0
    for entry in entries:
        count = len(entry.posts)
        total += count
        header = f"--- @{entry.channel} ({count} постов) ---"
        if entry.posts:
            posts_text = _format_posts(entry.posts, max_text=150)
            parts.append(f"{header}\n{posts_text}")
        else:
            parts.append(f"{header}\n(нет постов за этот период)")

    summary = f"Дайджест: {len(ch_list)} каналов, {total} постов, период: {period}\n\n"
    return summary + "\n\n".join(parts)


async def _tool_compare_channels(channels: str, limit: int = 30) -> str:
    """Сравнить несколько Telegram-каналов по метрикам (подписчики, просмотры, реакции, частота).

    Args:
        channels: Каналы через запятую.
        limit: Постов для анализа с каждого канала (по умолчанию 30).
    """
    ch_list = [c.strip() for c in channels.split(",") if c.strip()]
    if not ch_list:
        return "Ошибка: укажите хотя бы один канал."

    results = await compare_channels(ch_list, limit=min(limit, 100))

    lines = ["Канал | Подписчики | Avg Views | Avg Reactions | Posts/Week"]
    lines.append("-" * 70)
    for r in results:
        lines.append(
            f"@{r['username']} ({r['title']}) | {r['subscribers']} | "
            f"{r['avg_views']} | {r['avg_reactions']} | {r['posts_per_week']}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TELEGRAM_CHANNEL_TOOLS = [
    StructuredTool.from_function(
        coroutine=_tool_fetch_posts,
        name="tg_channel_fetch_posts",
        description="Получить последние посты из публичного Telegram-канала",
    ),
    StructuredTool.from_function(
        coroutine=_tool_channel_info,
        name="tg_channel_info",
        description="Получить информацию о публичном Telegram-канале (название, описание, подписчики)",
    ),
    StructuredTool.from_function(
        coroutine=_tool_search_posts,
        name="tg_channel_search",
        description="Поиск по постам публичного Telegram-канала",
    ),
    StructuredTool.from_function(
        coroutine=_tool_top_posts,
        name="tg_channel_top_posts",
        description="Топ постов канала по просмотрам или реакциям",
    ),
    StructuredTool.from_function(
        coroutine=_tool_digest,
        name="tg_channels_digest",
        description="Дайджест постов из нескольких Telegram-каналов за период (today/yesterday/week/N дней)",
    ),
    StructuredTool.from_function(
        coroutine=_tool_compare_channels,
        name="tg_channels_compare",
        description="Сравнить Telegram-каналы по метрикам (подписчики, просмотры, реакции, частота публикаций)",
    ),
]


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------

TELEGRAM_CHANNELS_SYSTEM_PROMPT = """Ты — Telegram Channels Agent в системе Kronos. Твоя задача — мониторинг, анализ и дайджесты публичных Telegram-каналов.

Возможности:
- Получать посты из любого публичного канала
- Искать по постам
- Формировать дайджесты из нескольких каналов
- Сравнивать каналы по engagement-метрикам
- Находить топовые посты (шер-парад)

Правила:
- Каналы принимаются в любом формате: @username, t.me/username, просто username
- Только публичные каналы (с web preview)
- Между запросами пауза 1.5с — не ускоряй
- При дайджесте: группируй по темам, выделяй самое важное
- При сравнении: давай конкретные цифры и выводы
- Язык: русский"""


def create_telegram_channels_agent():
    """Create Telegram channels agent with native tools.

    Unlike other agents that filter MCP tools by prefix,
    this agent uses its own StructuredTools (no MCP dependency).
    """
    model = get_model(ModelTier.LITE)

    agent = create_agent(
        model=model,
        tools=TELEGRAM_CHANNEL_TOOLS,
        system_prompt=TELEGRAM_CHANNELS_SYSTEM_PROMPT,
        name="telegram_channels_agent",
    )

    log.info("Telegram Channels agent created with %d tools", len(TELEGRAM_CHANNEL_TOOLS))
    return agent
