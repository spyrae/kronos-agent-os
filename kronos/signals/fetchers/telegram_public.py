"""Telegram public web-preview adapter."""

from __future__ import annotations

from kronos.signals.fetchers.base import FetchOptions, FetchResult, source_item
from kronos.signals.sources import SignalSource
from kronos.tools import telegram_channels


async def fetch_telegram_public_source(
    source: SignalSource,
    *,
    options: FetchOptions | None = None,
    fetch_posts=telegram_channels.fetch_posts,
) -> FetchResult:
    """Fetch public Telegram channel posts through t.me/s."""
    opts = options or FetchOptions()
    posts = await fetch_posts(source.handle, limit=opts.limit)
    items = tuple(_post_to_item(source, post) for post in posts if getattr(post, "text", ""))
    return FetchResult(source=source, items=items)


def _post_to_item(source: SignalSource, post) -> object:
    channel = source.handle.lstrip("@")
    url = f"https://t.me/{channel}/{post.id}" if channel and getattr(post, "id", None) else ""
    return source_item(
        source,
        title=_title_from_text(post.text),
        text=post.text,
        url=url,
        source_item_key=str(post.id),
        source_url=url,
        published_at=getattr(post, "date", ""),
        raw_payload={
            "id": getattr(post, "id", 0),
            "views": getattr(post, "views", ""),
            "reactions": getattr(post, "reactions", 0),
            "fwd_from": getattr(post, "fwd_from", ""),
            "fwd_link": getattr(post, "fwd_link", ""),
            "media_url": getattr(post, "media_url", ""),
        },
        importance_score=_telegram_importance(getattr(post, "views_numeric", 0), getattr(post, "reactions", 0)),
        confidence_score=45.0,
    )


def _title_from_text(text: str, limit: int = 90) -> str:
    compact = " ".join((text or "").split())
    return compact[:limit].rstrip()


def _telegram_importance(views: int, reactions: int) -> float:
    return min(100.0, reactions * 8 + views / 100)
