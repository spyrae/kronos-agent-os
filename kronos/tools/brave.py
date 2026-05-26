"""Brave Search REST API client for cron jobs.

Lightweight HTTP client that doesn't depend on MCP.
Used by cron tasks that need web search (news_monitor, etc.).

Automatically falls back to Exa Search when Brave returns 402 (quota
exceeded) or 429 (rate limited). The fallback is sticky for
``_QUOTA_BACKOFF_SECONDS`` so we don't keep hammering Brave after a
known-exhausted plan; afterwards Brave is retried once and if it
succeeds we resume using it.
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from kronos.config import settings
from kronos.tools import exa as _exa

log = logging.getLogger("kronos.tools.brave")

BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"

# Global rate limiter: track last request time
_last_request_time: float = 0.0
_MIN_INTERVAL = 1.5  # seconds between requests

# When Brave reports quota/rate exhaustion, skip it for this long and
# route all search() calls to Exa instead. 6h is short enough to catch
# monthly resets quickly, long enough to avoid wasted retries.
_QUOTA_BACKOFF_SECONDS = 6 * 3600
_brave_unavailable_until: float = 0.0


@dataclass
class SearchResult:
    title: str
    url: str
    description: str


def _fallback_to_exa(query: str, count: int, freshness: str, reason: str) -> list[SearchResult]:
    """Call Exa with the same Brave-compatible signature.

    Translates Exa's SearchResult to Brave's SearchResult so callers
    that expect ``kronos.tools.brave.SearchResult`` keep working
    (the two dataclasses have identical fields).
    """
    log.warning("Brave unavailable (%s) — falling back to Exa for '%s'", reason, query[:60])
    exa_results = _exa.search(query, count=count, freshness=freshness)
    return [
        SearchResult(title=r.title, url=r.url, description=r.description)
        for r in exa_results
    ]


def search(query: str, count: int = 10, freshness: str = "pd") -> list[SearchResult]:
    """Search Brave and return results.

    Args:
        query: Search query string.
        count: Number of results (max 20).
        freshness: Time filter — 'pd' (past day), 'pw' (past week), 'pm' (past month).

    Returns:
        List of SearchResult.
    """
    # If Brave was recently marked as quota-exhausted, skip it entirely.
    global _brave_unavailable_until
    now_mono = time.monotonic()
    if _brave_unavailable_until > now_mono:
        if settings.brave_api_key:
            return _fallback_to_exa(query, count, freshness, "quota cooldown")
        # No Brave key configured at all — go straight to Exa silently.
        return _exa.search(query, count=count, freshness=freshness)

    if not settings.brave_api_key:
        # No Brave key: try Exa instead of returning empty (preserves callers).
        return _fallback_to_exa(query, count, freshness, "no BRAVE_API_KEY")

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

    except urllib.error.HTTPError as e:
        if e.code in (402, 429):
            # Quota exceeded or rate-limited by Brave's billing layer:
            # remember this for the next 6h to avoid retrying every request.
            _brave_unavailable_until = time.monotonic() + _QUOTA_BACKOFF_SECONDS
            reason = "HTTP 402 quota" if e.code == 402 else "HTTP 429 rate-limit"
            return _fallback_to_exa(query, count, freshness, reason)
        log.error("Brave search failed: %s", e)
        return []
    except Exception as e:
        log.error("Brave search failed: %s", e)
        return []
