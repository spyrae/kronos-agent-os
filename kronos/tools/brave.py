"""Brave Search REST API client for cron jobs.

Lightweight HTTP client that doesn't depend on MCP.
Used by cron tasks that need web search (news_monitor, etc.).
"""

import json
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

from kronos.config import settings

log = logging.getLogger("kronos.tools.brave")

BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"

# Global rate limiter: track last request time
_last_request_time: float = 0.0
_MIN_INTERVAL = 1.5  # seconds between requests


@dataclass
class SearchResult:
    title: str
    url: str
    description: str


def search(query: str, count: int = 10, freshness: str = "pd") -> list[SearchResult]:
    """Search Brave and return results.

    Args:
        query: Search query string.
        count: Number of results (max 20).
        freshness: Time filter — 'pd' (past day), 'pw' (past week), 'pm' (past month).

    Returns:
        List of SearchResult.
    """
    if not settings.brave_api_key:
        log.warning("BRAVE_API_KEY not set, cannot search")
        return []

    # Rate limiting — wait if needed
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _last_request_time = time.monotonic()

    params = urllib.parse.urlencode({
        "q": query,
        "count": min(count, 20),
        "freshness": freshness,
    })
    url = f"{BRAVE_API_URL}?{params}"

    try:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": settings.brave_api_key,
            },
        )
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())

        results = []
        for item in data.get("web", {}).get("results", []):
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                description=item.get("description", ""),
            ))

        log.debug("Brave search '%s': %d results", query[:50], len(results))
        return results

    except Exception as e:
        log.error("Brave search failed: %s", e)
        return []
