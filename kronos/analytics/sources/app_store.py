"""App Store data source — iOS/Android ratings and reviews.

iOS basics (rating, version, current-version review count) come from the
public iTunes Search API. For the actual *list of recent reviews* — body,
territory, rating per review — we use the App Store Connect API which
requires JWT auth (ES256, .p8 private key).

Configure via env vars:
- ASC_KEY_ID, ASC_ISSUER_ID, ASC_PRIVATE_KEY_PATH for reviews fetch
- ANDROID_PACKAGE for google-play-scraper
"""

import json
import logging
import os
import time
import urllib.request

log = logging.getLogger("kronos.analytics.sources.app_store")

_TIMEOUT = 15

# App IDs (configure via env vars)
_IOS_APP_ID = os.environ.get("IOS_APP_ID", "6759391883")
_ANDROID_PACKAGE = os.environ.get("ANDROID_PACKAGE", "")

_ASC_BASE = "https://api.appstoreconnect.apple.com"
_ASC_JWT_TTL = 1200  # 20 minutes — max allowed by Apple


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


def _generate_asc_jwt() -> str | None:
    """Generate App Store Connect JWT (ES256). Returns None if not configured."""
    key_id = os.environ.get("ASC_KEY_ID")
    issuer = os.environ.get("ASC_ISSUER_ID")
    key_path = os.environ.get("ASC_PRIVATE_KEY_PATH")
    if not all([key_id, issuer, key_path]):
        return None
    try:
        import jwt  # PyJWT[crypto]
    except ImportError:
        log.warning("PyJWT[crypto] not installed — install with `pip install 'PyJWT[crypto]'`")
        return None
    try:
        with open(key_path, "rb") as f:
            private_key = f.read()
        payload = {
            "iss": issuer,
            "iat": int(time.time()),
            "exp": int(time.time()) + _ASC_JWT_TTL,
            "aud": "appstoreconnect-v1",
        }
        return jwt.encode(payload, private_key, algorithm="ES256", headers={"kid": key_id})
    except Exception as e:
        log.error("ASC JWT generation failed: %s", e)
        return None


def _fetch_ios_reviews() -> dict:
    """Fetch recent iOS reviews via App Store Connect API.

    Returns aggregated stats from up to 50 most recent reviews:
    - all-territories review count + average rating
    - 5 most recent review samples (rating, title, body, territory)
    Returns empty dict if ASC credentials are not configured (silent).
    """
    token = _generate_asc_jwt()
    if not token:
        return {}

    url = f"{_ASC_BASE}/v1/apps/{_IOS_APP_ID}/customerReviews?limit=50&sort=-createdDate"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
        data = json.loads(resp.read())
    except Exception as e:
        log.warning("ASC reviews fetch failed: %s", e)
        return {}

    reviews = data.get("data", [])
    if not reviews:
        return {"ios_recent_reviews_count": 0}

    ratings = [r.get("attributes", {}).get("rating") for r in reviews]
    ratings = [r for r in ratings if isinstance(r, int)]
    avg = round(sum(ratings) / len(ratings), 2) if ratings else None

    samples = []
    for r in reviews[:5]:
        a = r.get("attributes", {}) or {}
        samples.append(
            {
                "rating": a.get("rating"),
                "title": (a.get("title") or "")[:80],
                "body": (a.get("body") or "")[:300],
                "territory": a.get("territory"),
                "date": (a.get("createdDate") or "")[:10],
            }
        )

    return {
        "ios_recent_reviews_count": len(reviews),
        "ios_recent_avg_rating": avg,
        "ios_recent_reviews": samples,
    }


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

    # iOS recent reviews via ASC API (optional, requires .p8 key).
    try:
        result.update(_fetch_ios_reviews())
    except Exception as e:
        log.warning("ASC reviews fetch failed: %s", e)

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
