"""X/Twitter adapter using Brave/Exa site search fallback."""

from __future__ import annotations

from kronos.signals.fetchers.base import FetchOptions, FetchResult, source_item
from kronos.signals.sources import SignalSource
from kronos.tools import brave


async def fetch_x_source(
    source: SignalSource,
    *,
    options: FetchOptions | None = None,
    search_fn=brave.search,
) -> FetchResult:
    """Fetch X posts through search and normalize them as signal items."""
    opts = options or FetchOptions()
    query = _x_query(source)
    results = search_fn(query, count=opts.limit, freshness=opts.freshness)
    items = tuple(
        source_item(
            source,
            title=result.title,
            text=result.description,
            url=result.url,
            source_item_key=result.url,
            source_url=result.url,
            author=source.handle,
            handle=source.handle,
            raw_payload={
                "query": query,
                "title": result.title,
                "url": result.url,
                "description": result.description,
            },
            confidence_score=_confidence_for_x_source(source),
        )
        for result in results
        if result.url or result.title or result.description
    )
    return FetchResult(source=source, items=items)


def _x_query(source: SignalSource) -> str:
    handle = source.handle.lstrip("@")
    context = source.description or source.handle
    return f"site:x.com/{handle} {context}".strip()


def _confidence_for_x_source(source: SignalSource) -> float:
    if source.trust == "official":
        return 85.0
    if source.trust == "expert":
        return 65.0
    return 40.0
