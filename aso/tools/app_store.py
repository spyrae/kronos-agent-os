"""App Store Connect data collection for ASO pipeline.

Wraps App Store Connect API calls. Designed to run standalone
(direct API via httpx) or delegate to MCP tools when available.

Environment:
    ASC_KEY_ID        App Store Connect API Key ID
    ASC_ISSUER_ID     Issuer ID
    ASC_PRIVATE_KEY   Path to .p8 private key file
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import httpx

log = logging.getLogger("aso.tools.app_store")

ASC_BASE = "https://api.appstoreconnect.apple.com/v1"

# Public-safe defaults. Override with ASC_APP_ID and ASC_BUNDLE_ID.
APP_ID = os.environ.get("ASC_APP_ID", "")
BUNDLE_ID = os.environ.get("ASC_BUNDLE_ID", "com.example.app")


# --- JWT Auth ---

def _generate_jwt() -> str:
    """Generate App Store Connect JWT token.

    Uses ES256 algorithm with the .p8 private key.
    Token is valid for 20 minutes (Apple maximum).
    """
    import jwt  # PyJWT with cryptography backend

    key_id = os.environ["ASC_KEY_ID"]
    issuer_id = os.environ["ASC_ISSUER_ID"]
    key_path = os.environ["ASC_PRIVATE_KEY"]

    private_key = Path(key_path).read_text()

    now = int(time.time())
    payload = {
        "iss": issuer_id,
        "iat": now,
        "exp": now + 1200,  # 20 min
        "aud": "appstoreconnect-v1",
    }

    return jwt.encode(payload, private_key, algorithm="ES256", headers={"kid": key_id})


_cached_token: str | None = None
_token_exp: float = 0


def _get_token() -> str:
    global _cached_token, _token_exp
    if _cached_token and time.time() < _token_exp - 60:
        return _cached_token
    _cached_token = _generate_jwt()
    _token_exp = time.time() + 1200
    return _cached_token


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_get_token()}",
        "Content-Type": "application/json",
    }


# --- API Helpers ---

async def _get(path: str, params: dict | None = None) -> dict:
    """GET request to App Store Connect API."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{ASC_BASE}{path}", headers=_headers(), params=params)
        resp.raise_for_status()
        return resp.json()


async def _patch(path: str, payload: dict) -> dict:
    """PATCH request to App Store Connect API."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.patch(f"{ASC_BASE}{path}", headers=_headers(), json=payload)
        resp.raise_for_status()
        return resp.json()


# --- Public API ---

async def get_latest_version_id(app_id: str = APP_ID) -> tuple[str | None, str]:
    """Get the ID of the most relevant app version.

    Tries states in priority order:
    1. READY_FOR_SALE (live version)
    2. PREPARE_FOR_SUBMISSION (pre-launch / next version)
    3. WAITING_FOR_REVIEW, IN_REVIEW
    4. Any other state

    Returns (version_id, state) or (None, "").
    """
    # Try preferred states first
    preferred_states = [
        "READY_FOR_SALE",
        "PREPARE_FOR_SUBMISSION",
        "WAITING_FOR_REVIEW",
        "IN_REVIEW",
        "PENDING_DEVELOPER_RELEASE",
    ]

    for state in preferred_states:
        data = await _get(f"/apps/{app_id}/appStoreVersions", params={
            "filter[appStoreState]": state,
            "filter[platform]": "IOS",
            "limit": "1",
        })
        versions = data.get("data", [])
        if versions:
            version_id = versions[0]["id"]
            log.info("Found version %s in state %s", version_id, state)
            return version_id, state

    # Fallback: get any version
    data = await _get(f"/apps/{app_id}/appStoreVersions", params={
        "filter[platform]": "IOS",
        "limit": "1",
    })
    versions = data.get("data", [])
    if versions:
        attrs = versions[0].get("attributes", {})
        state = attrs.get("appStoreState", "UNKNOWN")
        return versions[0]["id"], state

    log.warning("No versions found for app %s", app_id)
    return None, ""


async def get_localizations(version_id: str) -> dict[str, dict]:
    """Get all localizations for a version.

    Returns {locale: {title, subtitle, keywords, description, ...}}.
    """
    data = await _get("/appStoreVersionLocalizations", params={
        "filter[appStoreVersion]": version_id,
        "limit": "50",
    })

    result = {}
    for item in data.get("data", []):
        attrs = item.get("attributes", {})
        locale = attrs.get("locale", "unknown")
        result[locale] = {
            "localization_id": item["id"],
            "locale": locale,
            "title": attrs.get("title", ""),
            "subtitle": attrs.get("subtitle", ""),
            "keywords": attrs.get("keywords", ""),
            "description": attrs.get("description", ""),
            "promotional_text": attrs.get("promotionalText", ""),
            "whats_new": attrs.get("whatsNew", ""),
            "marketing_url": attrs.get("marketingUrl", ""),
            "support_url": attrs.get("supportUrl", ""),
        }

    log.info("Fetched %d localizations for version %s", len(result), version_id)
    return result


async def get_current_metadata(app_id: str = APP_ID) -> dict[str, dict]:
    """Get metadata for the latest app version across all locales.

    Works for both live (READY_FOR_SALE) and pre-launch
    (PREPARE_FOR_SUBMISSION) versions.
    """
    version_id, version_state = await get_latest_version_id(app_id)
    if not version_id:
        return {}
    localizations = await get_localizations(version_id)
    # Inject version state into each locale for context
    for locale_data in localizations.values():
        locale_data["_version_state"] = version_state
        locale_data["_version_id"] = version_id
    return localizations


async def is_live(app_id: str = APP_ID) -> bool:
    """Check if the app is currently live in the App Store."""
    version_id, state = await get_latest_version_id(app_id)
    return state == "READY_FOR_SALE"


async def update_localization(localization_id: str, field: str, value: str) -> dict:
    """Update a single field in a version localization.

    Fields: description, keywords, marketingUrl, promotionalText, supportUrl, whatsNew.
    Note: title and subtitle require a different endpoint (appInfoLocalizations).
    """
    payload = {
        "data": {
            "type": "appStoreVersionLocalizations",
            "id": localization_id,
            "attributes": {
                field: value,
            },
        },
    }
    result = await _patch(f"/appStoreVersionLocalizations/{localization_id}", payload)
    log.info("Updated %s for localization %s", field, localization_id)
    return result


async def request_analytics_report(app_id: str = APP_ID) -> str:
    """Create an analytics report request. Returns report request ID."""
    payload = {
        "data": {
            "type": "analyticsReportRequests",
            "attributes": {
                "accessType": "ONE_TIME_SNAPSHOT",
            },
            "relationships": {
                "app": {
                    "data": {"type": "apps", "id": app_id},
                },
            },
        },
    }
    data = await _patch("/analyticsReportRequests", payload)
    request_id = data["data"]["id"]
    log.info("Analytics report requested: %s", request_id)
    return request_id


async def get_analytics_reports(request_id: str, category: str = "APP_STORE_ENGAGEMENT") -> list[dict]:
    """List available reports for a request, filtered by category."""
    data = await _get(f"/analyticsReportRequests/{request_id}/analyticsReports", params={
        "filter[category]": category,
        "limit": "50",
    })
    return data.get("data", [])


async def download_report_segments(report_id: str) -> list[dict]:
    """Get download URLs for report segments."""
    data = await _get(f"/analyticsReports/{report_id}/analyticsReportSegments", params={
        "limit": "50",
    })
    segments = []
    for item in data.get("data", []):
        attrs = item.get("attributes", {})
        if attrs.get("url"):
            segments.append({
                "id": item["id"],
                "url": attrs["url"],
                "checksum": attrs.get("checksum"),
                "size": attrs.get("sizeInBytes"),
            })
    return segments


async def download_segment_data(url: str) -> str:
    """Download raw data from a report segment URL."""
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text
