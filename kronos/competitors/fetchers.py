"""Data fetchers for App Store and Play Store."""

import asyncio
import logging

import aiohttp

from kronos.competitors.models import AppSnapshot

log = logging.getLogger("kronos.competitors.fetchers")

# Generous timeout for App Store API
_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Delay between requests to avoid rate limiting
_REQUEST_DELAY = 2.0


async def fetch_ios(ios_id: str) -> AppSnapshot | None:
    """Fetch app data from iTunes Search API (public, no auth required)."""
    if not ios_id:
        return None

    url = f"https://itunes.apple.com/lookup?id={ios_id}&country=us"

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    log.warning("iTunes API returned %d for %s", resp.status, ios_id)
                    return None

                data = await resp.json(content_type=None)

        results = data.get("results", [])
        if not results:
            log.warning("No results from iTunes API for %s", ios_id)
            return None

        app = results[0]
        return AppSnapshot(
            version=app.get("version", ""),
            rating=round(app.get("averageUserRating", 0.0), 2),
            rating_count=app.get("userRatingCount", 0),
            release_notes=app.get("releaseNotes", "")[:2000],
            last_updated=app.get("currentVersionReleaseDate", ""),
            price=app.get("price", 0.0),
            description=app.get("description", "")[:500],
            screenshots_count=len(app.get("screenshotUrls", [])),
            developer=app.get("artistName", ""),
        )

    except Exception as e:
        log.error("Failed to fetch iOS data for %s: %s", ios_id, e)
        return None


async def fetch_android(package: str) -> AppSnapshot | None:
    """Fetch app data from Google Play using google-play-scraper."""
    if not package:
        return None

    try:
        from google_play_scraper import app as gplay_app
        from google_play_scraper.exceptions import NotFoundError

        # google-play-scraper is sync — run in executor
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(
                None,
                lambda: gplay_app(package, lang="en", country="us"),
            )
        except NotFoundError:
            log.warning("Android app not found: %s", package)
            return None

        return AppSnapshot(
            version=data.get("version", "Unknown"),
            rating=round(data.get("score", 0.0), 2),
            rating_count=data.get("ratings", 0),
            release_notes=data.get("recentChanges", "")[:2000] if data.get("recentChanges") else "",
            last_updated=data.get("lastUpdatedOn", ""),
            price=data.get("price", 0.0),
            description=data.get("description", "")[:500],
            installs=data.get("installs", ""),
            developer=data.get("developer", ""),
        )

    except ImportError:
        log.warning("google-play-scraper not installed, skipping Android fetch")
        return None
    except Exception as e:
        log.error("Failed to fetch Android data for %s: %s", package, e)
        return None


async def fetch_all_for_competitor(
    ios_id: str,
    android_package: str,
) -> dict[str, AppSnapshot]:
    """Fetch iOS and Android data for a single competitor.

    Returns dict keyed by channel name ('app_store_ios', 'app_store_android').
    """
    results: dict[str, AppSnapshot] = {}

    if ios_id:
        snapshot = await fetch_ios(ios_id)
        if snapshot:
            results["app_store_ios"] = snapshot

    # Delay between iOS and Android to avoid rate limiting
    if ios_id and android_package:
        await asyncio.sleep(_REQUEST_DELAY)

    if android_package:
        snapshot = await fetch_android(android_package)
        if snapshot:
            results["app_store_android"] = snapshot

    return results
