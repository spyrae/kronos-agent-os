"""App Store data source — iOS/Android ratings and reviews."""

import json
import logging
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from kronos.config import settings

log = logging.getLogger("kronos.analytics.sources.app_store")

_TIMEOUT = 15

# App IDs (configure via env vars)
_IOS_APP_ID = "6759391883"
_ANDROID_PACKAGE = os.environ.get("ANDROID_PACKAGE", "")


def _fetch_ios() -> dict:
    """Fetch iOS app data from iTunes Search API (public, no auth)."""
    url = f"https://itunes.apple.com/lookup?id={_IOS_APP_ID}&country=us"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    data = json.loads(resp.read())

    results = data.get("results", [])
    if not results:
        return {"error": "iOS app not found"}

    app = results[0]
    return {
        "ios_rating": app.get("averageUserRating"),
        "ios_reviews_count": app.get("userRatingCount"),
        "ios_version": app.get("version"),
        "ios_release_notes": (app.get("releaseNotes") or "")[:200],
    }


def _fetch_android() -> dict:
    """Fetch Android app data from Google Play Store."""
    try:
        from google_play_scraper import app as gplay_app
    except ImportError:
        return {"error": "google-play-scraper not installed"}

    try:
        data = gplay_app(_ANDROID_PACKAGE, lang="en", country="us")
        return {
            "android_rating": round(data.get("score", 0), 2) if data.get("score") else None,
            "android_reviews_count": data.get("reviews"),
            "android_installs": data.get("realInstalls"),
            "android_version": data.get("version"),
        }
    except Exception as e:
        log.debug("Play Store fetch failed: %s", e)
        return {"error": str(e)}


def collect() -> dict:
    """Collect app store metrics for daily pulse."""
    result = {}

    # iOS — always available (public API)
    try:
        ios = _fetch_ios()
        result.update(ios)
    except Exception as e:
        log.error("iOS fetch failed: %s", e)
        result["ios_error"] = str(e)

    # Android — google-play-scraper is sync, run directly
    try:
        android = _fetch_android()
        result.update(android)
    except Exception as e:
        log.error("Android fetch failed: %s", e)
        result["android_error"] = str(e)

    # At least one platform should work
    if "error" in result and "ios_rating" not in result and "android_rating" not in result:
        return {"error": "Both app stores failed"}

    return result
