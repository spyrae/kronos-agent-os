"""Keyword position tracking for ASO.

Phase 1: Uses iTunes Search API + web search as free proxy.
Future: Apify actors for real App Store search scraping.

Environment:
    ASO_KEYWORDS_FILE   Path to keywords config (YAML/JSON)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx

log = logging.getLogger("aso.tools.keyword_tracker")

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"

# Default keywords config location
DEFAULT_KEYWORDS_FILE = Path(__file__).parent.parent / "config" / "keywords.json"


def load_target_keywords() -> list[dict]:
    """Load target keywords from config file.

    Format: [{"keyword": "travel planner", "locale": "en", "priority": "high"}, ...]
    """
    keywords_file = os.environ.get("ASO_KEYWORDS_FILE", str(DEFAULT_KEYWORDS_FILE))
    path = Path(keywords_file)

    if not path.exists():
        log.warning("Keywords file not found: %s — using defaults", path)
        return _default_keywords()

    return json.loads(path.read_text())


def _default_keywords() -> list[dict]:
    """Baseline keywords for the configured app."""
    return [
        {"keyword": "travel planner", "locale": "en", "priority": "high"},
        {"keyword": "trip planner", "locale": "en", "priority": "high"},
        {"keyword": "itinerary", "locale": "en", "priority": "high"},
        {"keyword": "travel organizer", "locale": "en", "priority": "medium"},
        {"keyword": "vacation planner", "locale": "en", "priority": "medium"},
        {"keyword": "travel app", "locale": "en", "priority": "medium"},
        {"keyword": "планировщик путешествий", "locale": "ru", "priority": "high"},
        {"keyword": "планирование поездки", "locale": "ru", "priority": "high"},
        {"keyword": "маршрут путешествия", "locale": "ru", "priority": "medium"},
    ]


async def search_itunes(term: str, *, country: str = "us", limit: int = 50) -> list[dict]:
    """Search iTunes API and return app results.

    Free, no auth required. Returns catalog results (not actual App Store search ranking).
    Useful as a proxy: if the app doesn't appear in top-50, ranking is likely low.
    """
    params = {
        "term": term,
        "country": country,
        "media": "software",
        "limit": str(limit),
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(ITUNES_SEARCH_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    return data.get("results", [])


async def find_app_position(
    term: str,
    bundle_id: str = "com.example.app",
    *,
    country: str = "us",
) -> dict:
    """Search for a keyword and find our app's position.

    Returns:
        {
            "keyword": str,
            "country": str,
            "position": int | None,  # 1-based, None if not found in top-50
            "total_results": int,
            "found": bool,
        }
    """
    results = await search_itunes(term, country=country)

    position = None
    for i, app in enumerate(results):
        if app.get("bundleId") == bundle_id:
            position = i + 1
            break

    return {
        "keyword": term,
        "country": country,
        "position": position,
        "total_results": len(results),
        "found": position is not None,
    }


async def track_all_keywords(bundle_id: str = "com.example.app") -> dict[str, dict]:
    """Track positions for all target keywords.

    Returns {keyword: {position, country, found, ...}}.
    """
    keywords = load_target_keywords()
    results = {}

    locale_to_country = {
        "en": "us",
        "ru": "ru",
        "de": "de",
        "fr": "fr",
        "es": "es",
        "ja": "jp",
        "ko": "kr",
        "zh": "cn",
    }

    for kw in keywords:
        term = kw["keyword"]
        locale = kw.get("locale", "en")
        country = locale_to_country.get(locale, "us")

        try:
            result = await find_app_position(term, bundle_id, country=country)
            result["priority"] = kw.get("priority", "medium")
            results[f"{term}_{country}"] = result
        except Exception as e:
            log.warning("Failed to track '%s' (%s): %s", term, country, e)
            results[f"{term}_{country}"] = {
                "keyword": term,
                "country": country,
                "position": None,
                "found": False,
                "error": str(e),
                "priority": kw.get("priority", "medium"),
            }

    found_count = sum(1 for r in results.values() if r.get("found"))
    log.info("Tracked %d keywords: %d found in top-50", len(results), found_count)
    return results


async def get_app_info(bundle_id: str = "com.example.app", country: str = "us") -> dict | None:
    """Lookup app info from iTunes API (ratings, reviews count, etc)."""
    params = {
        "bundleId": bundle_id,
        "country": country,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(ITUNES_LOOKUP_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    results = data.get("results", [])
    if not results:
        return None

    app = results[0]
    return {
        "track_id": app.get("trackId"),
        "name": app.get("trackName"),
        "average_rating": app.get("averageUserRating"),
        "rating_count": app.get("userRatingCount"),
        "current_version_rating": app.get("averageUserRatingForCurrentVersion"),
        "current_version_rating_count": app.get("userRatingCountForCurrentVersion"),
        "version": app.get("version"),
        "price": app.get("price"),
        "genre": app.get("primaryGenreName"),
        "genres": app.get("genres"),
        "content_rating": app.get("contentAdvisoryRating"),
        "file_size": app.get("fileSizeBytes"),
        "release_date": app.get("releaseDate"),
        "current_version_release_date": app.get("currentVersionReleaseDate"),
    }
