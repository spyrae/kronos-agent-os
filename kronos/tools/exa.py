"""Exa Search REST API client.

Mirrors kronos.tools.brave.search() so it can be used as a fallback
when Brave quota is exhausted (HTTP 402). See brave.search() for the
automatic fallback logic.

Exa uses semantic neural search, which often returns better results
than keyword-based engines for AI/RAG-style queries.

Docs: https://docs.exa.ai/reference/search
"""

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

log = logging.getLogger("kronos.tools.exa")

EXA_API_URL = "https://api.exa.ai/search"

# Global rate limiter (Exa free tier ~1 req/sec is safe; paid up to ~10 req/sec)
_last_request_time: float = 0.0
_MIN_INTERVAL = 0.5


@dataclass
class SearchResult:
    title: str
    url: str
    description: str


def _freshness_to_start_date(freshness: str) -> str | None:
    """Map Brave-style freshness codes to Exa start_published_date (ISO).

    'pd' (past day) -> now - 1d
    'pw' (past week) -> now - 7d
    'pm' (past month) -> now - 30d
    Anything else -> None (no date filter).
    """
    delta_days = {"pd": 1, "pw": 7, "pm": 30}.get(freshness)
    if delta_days is None:
        return None
    return (datetime.now(UTC) - timedelta(days=delta_days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def search(query: str, count: int = 10, freshness: str = "pd") -> list[SearchResult]:
    """Search Exa and return results.

    Args:
        query: Search query string.
        count: Number of results (max 100; Exa default 10).
        freshness: Time filter (Brave-compatible) — 'pd', 'pw', 'pm', or '' for none.

    Returns:
        List of SearchResult.
    """
    api_key = os.environ.get("EXA_API_KEY", "")
    if not api_key:
        log.warning("EXA_API_KEY not set, cannot search")
        return []

    # Rate limiting
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _last_request_time = time.monotonic()

    body: dict = {
        "query": query,
        "numResults": min(count, 100),
        # Use auto: Exa picks neural vs keyword based on query
        "type": "auto",
    }
    start_date = _freshness_to_start_date(freshness)
    if start_date:
        body["startPublishedDate"] = start_date

    try:
        req = urllib.request.Request(
            EXA_API_URL,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "x-api-key": api_key,
            },
        )
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())

        results = []
        for item in data.get("results", []):
            results.append(SearchResult(
                title=item.get("title") or "",
                url=item.get("url") or "",
                # Exa returns text/highlight; prefer summary if present
                description=(item.get("summary")
                             or item.get("text")
                             or item.get("highlights", [""])[0]
                             or "")[:300],
            ))

        log.debug("Exa search '%s': %d results", query[:50], len(results))
        return results

    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read()[:200].decode("utf-8", errors="replace")
        except Exception:
            pass
        log.error("Exa search failed: HTTP %d %s — %s", e.code, e.reason, body_text)
        return []
    except Exception as e:
        log.error("Exa search failed: %s", e)
        return []
