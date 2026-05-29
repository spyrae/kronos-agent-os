"""Telegram Telethon adapter reusing the legacy group digest fetcher."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from kronos.signals.fetchers.base import FetchOptions, FetchResult, source_item
from kronos.signals.sources import SignalSource

MessageFetcher = Callable[[str, int], Awaitable[list[dict]]]


async def fetch_telegram_telethon_source(
    source: SignalSource,
    *,
    options: FetchOptions | None = None,
    message_fetcher: MessageFetcher | None = None,
) -> FetchResult:
    """Fetch Telegram messages via the configured Telethon userbot."""
    opts = options or FetchOptions()
    fetcher = message_fetcher or _default_message_fetcher
    messages = await fetcher(source.handle, int(source.filters.get("lookback_hours", 24)))
    messages = _filter_messages(
        messages,
        min_reactions=int(source.filters.get("min_reactions") or 0),
        min_views=int(source.filters.get("min_views") or 0),
    )
    items = tuple(_message_to_item(source, message) for message in messages[: opts.limit] if message.get("text"))
    return FetchResult(source=source, items=items)


async def _default_message_fetcher(source_id: str, hours: int) -> list[dict]:
    from kronos.cron.group_digest import _fetch_messages

    return await _fetch_messages(source_id, hours=hours)


def _message_to_item(source: SignalSource, message: dict) -> object:
    url = str(message.get("post_link") or "")
    text = str(message.get("text") or "")
    return source_item(
        source,
        title=_title_from_text(text),
        text=text,
        url=url,
        source_item_key=url or _title_from_text(text, limit=120),
        source_url=url,
        author=str(message.get("author") or ""),
        published_at=str(message.get("date") or ""),
        raw_payload={
            "reactions": int(message.get("reactions") or 0),
            "views": int(message.get("views") or 0),
            "urls": list(message.get("urls") or []),
        },
        importance_score=_telegram_importance(
            views=int(message.get("views") or 0),
            reactions=int(message.get("reactions") or 0),
        ),
        confidence_score=45.0,
    )


def _title_from_text(text: str, limit: int = 90) -> str:
    compact = " ".join((text or "").split())
    return compact[:limit].rstrip()


def _filter_messages(messages: list[dict], *, min_reactions: int, min_views: int) -> list[dict]:
    if min_reactions <= 0 and min_views <= 0:
        return messages
    scored = sorted(
        messages,
        key=lambda message: _telegram_importance(
            views=int(message.get("views") or 0),
            reactions=int(message.get("reactions") or 0),
        ),
        reverse=True,
    )
    return [
        message
        for message in scored
        if int(message.get("reactions") or 0) >= min_reactions
        or int(message.get("views") or 0) >= min_views
    ]


def _telegram_importance(views: int, reactions: int) -> float:
    return min(100.0, reactions * 8 + views / 100)
