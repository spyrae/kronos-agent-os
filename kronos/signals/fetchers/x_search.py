"""X/Twitter adapter with official API primary and strict status fallback."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlparse

from kronos.signals.fetchers.base import FetchOptions, FetchResult, source_item
from kronos.signals.sources import SignalSource
from kronos.tools import brave

X_RECENT_SEARCH_URL = "https://api.twitter.com/2/tweets/search/recent"
X_STATUS_HOSTS = {"x.com", "twitter.com"}


async def fetch_x_source(
    source: SignalSource,
    *,
    options: FetchOptions | None = None,
    search_fn=brave.search,
    x_api_fn: Callable[..., Mapping[str, Any]] | None = None,
    bearer_token: str | None = None,
) -> FetchResult:
    """Fetch real X status items with graceful fallback.

    Strategy:
    1. Official X API when ``X_BEARER_TOKEN`` or ``bearer_token`` is set.
    2. Strict search fallback that accepts only matching X/Twitter status URLs.
    3. Empty result on unavailable backends; no secondary articles are emitted
       as X items.
    """
    opts = options or FetchOptions()
    token = bearer_token if bearer_token is not None else os.environ.get("X_BEARER_TOKEN", "")
    handle = _normalized_handle(source)

    if token:
        try:
            payload = (x_api_fn or _fetch_recent_search)(source, opts, token, handle=handle)
            items = _items_from_x_api_payload(source, payload, handle=handle, limit=opts.limit)
            return FetchResult(source=source, items=items)
        except (ConnectionError, PermissionError, TimeoutError, urllib.error.URLError):
            pass
        except Exception:
            pass

    try:
        items = _strict_search_fallback(source, opts, handle=handle, search_fn=search_fn)
    except Exception:
        items = ()
    return FetchResult(source=source, items=items)


def _fetch_recent_search(
    source: SignalSource,
    options: FetchOptions,
    bearer_token: str,
    *,
    handle: str,
) -> Mapping[str, Any]:
    query = _x_api_query(source, handle=handle)
    params = urllib.parse.urlencode(
        {
            "query": query,
            "max_results": max(10, min(int(options.limit or 10), 100)),
            "tweet.fields": "created_at,public_metrics,entities,author_id,lang",
        }
    )
    request = urllib.request.Request(
        f"{X_RECENT_SEARCH_URL}?{params}",
        headers={"Authorization": f"Bearer {bearer_token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise PermissionError("X API auth failed") from exc
        raise ConnectionError(f"X API error HTTP {exc.code}") from exc


def _items_from_x_api_payload(
    source: SignalSource,
    payload: Mapping[str, Any],
    *,
    handle: str,
    limit: int,
) -> tuple:
    items = []
    for tweet in list(payload.get("data") or [])[:limit]:
        if not isinstance(tweet, Mapping):
            continue
        tweet_id = str(tweet.get("id") or "").strip()
        if not tweet_id.isdecimal():
            continue
        text = _compact(str(tweet.get("text") or ""))
        canonical_url = _canonical_status_url(handle, tweet_id)
        metrics = dict(tweet.get("public_metrics") or {})
        items.append(
            source_item(
                source,
                title=_title_from_text(text),
                text=text,
                url=canonical_url,
                source_item_key=canonical_url,
                source_url=canonical_url,
                author=f"@{handle}",
                handle=f"@{handle}",
                published_at=str(tweet.get("created_at") or ""),
                raw_payload={
                    "backend": "official_x_api",
                    "source_platform": "x",
                    "author_handle": f"@{handle}",
                    "status_id": tweet_id,
                    "canonical_url": canonical_url,
                    "created_at": tweet.get("created_at") or "",
                    "public_metrics": metrics,
                    "entities": tweet.get("entities") or {},
                    "raw_api_payload": dict(tweet),
                },
                importance_score=_importance_from_metrics(metrics),
                confidence_score=_confidence_for_x_source(source),
            )
        )
    return tuple(items)


def _strict_search_fallback(
    source: SignalSource,
    options: FetchOptions,
    *,
    handle: str,
    search_fn=brave.search,
) -> tuple:
    query = _strict_status_query(source, handle=handle)
    results = search_fn(query, count=options.limit, freshness=options.freshness)
    items = []
    seen: set[str] = set()
    for result in results:
        parsed = _parse_matching_status_url(str(getattr(result, "url", "") or ""), handle=handle)
        if parsed is None:
            continue
        status_handle, status_id = parsed
        canonical_url = _canonical_status_url(status_handle, status_id)
        if canonical_url in seen:
            continue
        seen.add(canonical_url)
        title = _compact(str(getattr(result, "title", "") or ""))
        description = _compact(str(getattr(result, "description", "") or ""))
        items.append(
            source_item(
                source,
                title=title or f"@{status_handle} status {status_id}",
                text=description,
                url=canonical_url,
                source_item_key=canonical_url,
                source_url=canonical_url,
                author=f"@{status_handle}",
                handle=f"@{status_handle}",
                raw_payload={
                    "backend": "strict_exa_status_fallback",
                    "query": query,
                    "original_url": str(getattr(result, "url", "") or ""),
                    "canonical_url": canonical_url,
                    "status_id": status_id,
                    "author_handle": f"@{status_handle}",
                    "title": title,
                    "description": description,
                },
                confidence_score=_confidence_for_x_source(source),
            )
        )
    return tuple(items)


def _parse_matching_status_url(url: str, *, handle: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    host = parsed.netloc.casefold().removeprefix("www.")
    if host not in X_STATUS_HOSTS:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3 or parts[1].casefold() != "status":
        return None
    url_handle = parts[0].lstrip("@").casefold()
    if url_handle != handle.casefold():
        return None
    status_id = parts[2]
    if not status_id.isdecimal():
        return None
    return handle, status_id


def _x_api_query(source: SignalSource, *, handle: str) -> str:
    terms = [f"from:{handle}", "-is:retweet"]
    if not bool(source.filters.get("include_replies", False)):
        terms.append("-is:reply")
    if source.language:
        terms.append(f"lang:{source.language}")
    extra_query = str(source.filters.get("query") or "").strip()
    if extra_query:
        terms.append(extra_query)
    return " ".join(terms)


def _strict_status_query(source: SignalSource, *, handle: str) -> str:
    context = source.description or source.handle or handle
    return f"site:x.com/{handle}/status {context}".strip()


def _normalized_handle(source: SignalSource) -> str:
    handle = (source.handle or source.url or source.id).strip().lstrip("@")
    if "/" in handle:
        handle = handle.rstrip("/").split("/")[-1]
    return handle


def _canonical_status_url(handle: str, status_id: str) -> str:
    return f"https://x.com/{handle.lstrip('@')}/status/{status_id}"


def _title_from_text(text: str) -> str:
    return text[:117] + "..." if len(text) > 120 else text


def _importance_from_metrics(metrics: Mapping[str, Any]) -> float:
    likes = _number(metrics.get("like_count"))
    reposts = _number(metrics.get("retweet_count"))
    replies = _number(metrics.get("reply_count"))
    quotes = _number(metrics.get("quote_count"))
    views = _number(metrics.get("impression_count"))
    score = 35.0 + min(35.0, likes * 0.03 + reposts * 0.12 + replies * 0.08 + quotes * 0.08 + views * 0.0005)
    return round(score, 2)


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _compact(text: str) -> str:
    return " ".join((text or "").split())


def _confidence_for_x_source(source: SignalSource) -> float:
    if source.trust == "official":
        return 85.0
    if source.trust == "expert":
        return 65.0
    return 40.0
