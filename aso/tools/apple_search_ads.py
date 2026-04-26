"""Apple Search Ads API — free keyword intelligence.

No ad spend required. Provides:
- Keyword popularity scores (1-100)
- Suggested keywords for an app
- Search volume estimates

Environment:
    APPLE_SEARCH_ADS_ORG_ID      Organization ID
    APPLE_SEARCH_ADS_KEY_ID      API Key ID
    APPLE_SEARCH_ADS_TEAM_ID     Team ID
    APPLE_SEARCH_ADS_CLIENT_ID   Client ID
    APPLE_SEARCH_ADS_SECRET      Client Secret
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("aso.tools.apple_search_ads")

ASA_BASE = "https://api.searchads.apple.com/api/v5"
ASA_AUTH_URL = "https://appleid.apple.com/auth/oauth2/token"


_cached_token: str | None = None
_token_exp: float = 0


async def _get_access_token() -> str:
    """Get OAuth2 token for Apple Search Ads API."""
    global _cached_token, _token_exp

    import time

    if _cached_token and time.time() < _token_exp - 60:
        return _cached_token

    client_id = os.environ.get("APPLE_SEARCH_ADS_CLIENT_ID", "")
    client_secret = os.environ.get("APPLE_SEARCH_ADS_SECRET", "")

    if not client_id or not client_secret:
        raise RuntimeError("Apple Search Ads credentials not set")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            ASA_AUTH_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "searchadsorg",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        data = resp.json()

    _cached_token = data["access_token"]
    _token_exp = time.time() + data.get("expires_in", 3600)
    return _cached_token


async def _headers() -> dict[str, str]:
    token = await _get_access_token()
    org_id = os.environ.get("APPLE_SEARCH_ADS_ORG_ID", "")
    return {
        "Authorization": f"Bearer {token}",
        "X-AP-Context": f"orgId={org_id}",
        "Content-Type": "application/json",
    }


async def get_keyword_popularity(
    keywords: list[str],
    *,
    storefront: str = "US",
) -> dict[str, dict]:
    """Get popularity scores for a list of keywords.

    Returns {keyword: {popularity: 1-100, ...}}.

    The popularity score reflects relative search volume:
    - 1-20: very low
    - 21-40: low
    - 41-60: medium
    - 61-80: high
    - 81-100: very high
    """
    if not keywords:
        return {}

    try:
        headers = await _headers()
    except RuntimeError:
        log.warning("Apple Search Ads not configured, skipping popularity lookup")
        return {}

    results = {}

    # API accepts batches — process in chunks of 50
    for i in range(0, len(keywords), 50):
        batch = keywords[i : i + 50]

        payload = {
            "keywords": batch,
            "storefront": storefront,
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{ASA_BASE}/keywords/volsearch",
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()

            for item in data.get("data", []):
                kw = item.get("searchTermText", "")
                results[kw] = {
                    "popularity": item.get("searchPopularity", 0),
                    "rank": _popularity_rank(item.get("searchPopularity", 0)),
                }

        except Exception as e:
            log.warning("Keyword popularity batch failed: %s", e)
            for kw in batch:
                results[kw] = {"popularity": None, "error": str(e)}

    log.info("Fetched popularity for %d keywords", len(results))
    return results


async def get_suggested_keywords(
    app_id: str,
    *,
    storefront: str = "US",
    limit: int = 50,
) -> list[dict]:
    """Get keyword suggestions for an app.

    Apple suggests keywords based on the app's metadata and category.
    Very useful for pre-launch keyword research.
    """
    try:
        headers = await _headers()
    except RuntimeError:
        log.warning("Apple Search Ads not configured, skipping suggestions")
        return []

    payload = {
        "adamId": app_id,
        "storefront": storefront,
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{ASA_BASE}/keywords/targeting",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        suggestions = []
        for item in data.get("data", [])[:limit]:
            suggestions.append({
                "keyword": item.get("searchTermText", ""),
                "popularity": item.get("searchPopularity", 0),
                "rank": _popularity_rank(item.get("searchPopularity", 0)),
            })

        log.info("Got %d keyword suggestions for app %s", len(suggestions), app_id)
        return suggestions

    except Exception as e:
        log.error("Keyword suggestions failed: %s", e)
        return []


def _popularity_rank(score: int) -> str:
    """Convert numeric popularity to human-readable rank."""
    if score >= 80:
        return "very_high"
    elif score >= 60:
        return "high"
    elif score >= 40:
        return "medium"
    elif score >= 20:
        return "low"
    else:
        return "very_low"
