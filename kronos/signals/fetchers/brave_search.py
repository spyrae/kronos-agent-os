"""Brave/Exa search adapter for generic search sources."""

from __future__ import annotations

from kronos.signals.fetchers.base import FetchOptions, FetchResult, source_item
from kronos.signals.sources import SignalSource
from kronos.tools import brave


async def fetch_search_source(
    source: SignalSource,
    *,
    options: FetchOptions | None = None,
    search_fn=brave.search,
) -> FetchResult:
    """Fetch a generic search source and normalize web results."""
    opts = options or FetchOptions()
    query = source.query or source.handle or source.description
    results = search_fn(query, count=opts.limit, freshness=opts.freshness)
    items = tuple(
        source_item(
            source,
            title=result.title,
            text=result.description,
            url=result.url,
            source_item_key=result.url,
            source_url=result.url,
            raw_payload={
                "query": query,
                "title": result.title,
                "url": result.url,
                "description": result.description,
            },
            confidence_score=_confidence_for_search_source(source),
        )
        for result in results
        if result.url or result.title or result.description
    )
    return FetchResult(source=source, items=items)


def _confidence_for_search_source(source: SignalSource) -> float:
    if source.trust == "official":
        return 80.0
    if source.tier == "core":
        return 65.0
    return 45.0
