"""Google SERP tracker — finds position of a target URL for a keyword.

Provider chain (try each until one returns non-empty results):

1. **Brave Web Search** (``BRAVE_API_KEY``) — primary, cheap (free tier
   2k req/mo, paid $9/mo for 20k). Google-like organic overlap.
2. **Serper.dev** (``SERPER_API_KEY``) — fallback, true Google SERP,
   $50/mo for 2.5k/day. Used when Brave returns 402/429 or empty.
3. **EXA** (``EXA_API_KEY``) — last-resort neural search, useful when
   both keyword-search APIs are down.

Returns ``None`` if the target URL is not in top results across all
providers.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from urllib.error import HTTPError

log = logging.getLogger("kronos.seo_geo.trackers.google")

_TIMEOUT = 15


def _brave_search(query: str, country: str = "us", count: int = 20) -> list[dict]:
    """Brave Search API → list of organic results.

    country: 'us' for google.com, 'ru' for google.ru.
    """
    api_key = os.environ.get("BRAVE_API_KEY") or ""
    if not api_key:
        return []

    params = {"q": query, "count": str(count), "country": country, "safesearch": "off"}
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "X-Subscription-Token": api_key,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; KronosNexus/1.0)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except HTTPError as e:
        log.warning("Brave search HTTP %d for %r", e.code, query[:60])
        return []
    except Exception as e:
        log.warning("Brave search failed for %r: %s", query[:60], e)
        return []

    web = data.get("web") or {}
    return [{"url": r.get("url"), "title": r.get("title")} for r in (web.get("results") or [])]


def _serper_search(query: str, country: str = "us", count: int = 20) -> list[dict]:
    """Serper.dev → real Google SERP organic results."""
    api_key = os.environ.get("SERPER_API_KEY") or ""
    if not api_key:
        return []

    body = json.dumps(
        {
            "q": query,
            "num": count,
            "gl": country,  # geolocation: 'us' / 'ru'
        }
    ).encode()
    req = urllib.request.Request(
        "https://google.serper.dev/search",
        data=body,
        method="POST",
        headers={
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except HTTPError as e:
        log.warning("Serper HTTP %d for %r", e.code, query[:60])
        return []
    except Exception as e:
        log.warning("Serper failed for %r: %s", query[:60], e)
        return []

    return [{"url": r.get("link"), "title": r.get("title")} for r in (data.get("organic") or [])]


def _exa_search(query: str, count: int = 20) -> list[dict]:
    """EXA neural search fallback when Brave 402/429."""
    api_key = os.environ.get("EXA_API_KEY") or ""
    if not api_key:
        return []
    body = json.dumps(
        {
            "query": query,
            "numResults": count,
            "type": "auto",
        }
    ).encode()
    req = urllib.request.Request(
        "https://api.exa.ai/search",
        data=body,
        method="POST",
        headers={
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log.warning("EXA search failed for %r: %s", query[:60], e)
        return []
    return data.get("results") or []


def find_position(target_url: str, query: str, locale: str = "en") -> tuple[int | None, str | None]:
    """Return (position_1_indexed, ranked_url) or (None, None) if not in top results.

    Provider chain: Brave → Serper → EXA. Stops at the first provider
    that returns non-empty results.
    """
    country = "ru" if locale == "ru" else "us"
    target_host = urllib.parse.urlparse(target_url).netloc.lower().replace("www.", "")
    if not target_host:
        return None, None

    # 1) Brave (primary, cheap)
    results = _brave_search(query, country=country, count=20)
    # 2) Serper (fallback, true Google SERP)
    if not results:
        results = _serper_search(query, country=country, count=20)
    # 3) EXA (last-resort neural search)
    if not results:
        results = _exa_search(query, count=20)

    for idx, r in enumerate(results, start=1):
        result_url = (r.get("url") or "").lower()
        if target_host in result_url:
            return idx, r.get("url")
    return None, None


def engine_id(locale: str) -> str:
    """Return the engine identifier used in the store."""
    return "google_ru" if locale == "ru" else "google_com"
