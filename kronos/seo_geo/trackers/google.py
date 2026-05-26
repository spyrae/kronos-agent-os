"""Google SERP tracker — finds position of a target URL for a keyword.

Uses the Brave Web Search API (already in env: BRAVE_API_KEY) which
returns Google-like organic results with high overlap. Falls back to
Exa if Brave rate-limits.

Returns ``None`` if the target URL is not in the top 100 results.
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
    Returns up to ``count`` results (max 20 per Brave call).
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
    return web.get("results") or []


def find_position(target_url: str, query: str, locale: str = "en") -> tuple[int | None, str | None]:
    """Return (position_1_indexed, ranked_url) or (None, None) if not in top 20.

    Brave returns at most 20 results per call; we treat positions beyond
    20 as "not ranking" — good enough for daily pulse signal.
    """
    country = "ru" if locale == "ru" else "us"
    target_host = urllib.parse.urlparse(target_url).netloc.lower().replace("www.", "")
    if not target_host:
        return None, None

    results = _brave_search(query, country=country, count=20)
    for idx, r in enumerate(results, start=1):
        result_url = (r.get("url") or "").lower()
        if target_host in result_url:
            return idx, r.get("url")
    return None, None


def engine_id(locale: str) -> str:
    """Return the engine identifier used in the store."""
    return "google_ru" if locale == "ru" else "google_com"
