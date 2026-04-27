"""Google Play Developer API client for ASO pipeline.

Direct API access for metadata management (listings CRUD).
Play Store MCP only supports releases, not metadata.

Uses the Edits API flow:
    1. edits.insert() → create a new edit (draft)
    2. edits.listings.get/update() → read/modify metadata
    3. edits.commit() → publish changes

Environment:
    PLAY_SERVICE_ACCOUNT_JSON   Path to Google Cloud service account JSON
    PLAY_PACKAGE_NAME           Package name (default: com.example.app)
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx

log = logging.getLogger("aso.tools.play_store")

PLAY_BASE = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications"
DEFAULT_PACKAGE = os.environ.get("PLAY_PACKAGE_NAME", "com.example.app")


# --- Auth via Service Account ---

_cached_access_token: str | None = None
_token_exp: float = 0


async def _get_access_token() -> str:
    """Get OAuth2 access token via service account JWT assertion.

    Uses Google's OAuth2 token endpoint with a self-signed JWT.
    """
    global _cached_access_token, _token_exp

    if _cached_access_token and time.time() < _token_exp - 60:
        return _cached_access_token

    sa_path = os.environ.get("PLAY_SERVICE_ACCOUNT_JSON")
    if not sa_path:
        raise RuntimeError("PLAY_SERVICE_ACCOUNT_JSON not set")

    sa_data = json.loads(Path(sa_path).read_text())

    import jwt  # PyJWT

    now = int(time.time())
    payload = {
        "iss": sa_data["client_email"],
        "scope": "https://www.googleapis.com/auth/androidpublisher",
        "aud": "https://oauth2.googleapis.com/token",
        "iat": now,
        "exp": now + 3600,
    }

    assertion = jwt.encode(payload, sa_data["private_key"], algorithm="RS256")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
        resp.raise_for_status()
        token_data = resp.json()

    _cached_access_token = token_data["access_token"]
    _token_exp = time.time() + token_data.get("expires_in", 3600)
    log.info("Google OAuth2 token refreshed")
    return _cached_access_token


async def _headers() -> dict[str, str]:
    token = await _get_access_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


# --- API Helpers ---

async def _get(path: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{PLAY_BASE}{path}", headers=await _headers())
        resp.raise_for_status()
        return resp.json()


async def _post(path: str, payload: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{PLAY_BASE}{path}",
            headers=await _headers(),
            json=payload or {},
        )
        resp.raise_for_status()
        return resp.json()


async def _put(path: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(
            f"{PLAY_BASE}{path}",
            headers=await _headers(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


# --- Edits API ---

async def create_edit(package: str = DEFAULT_PACKAGE) -> str:
    """Create a new edit (draft). Returns edit ID."""
    data = await _post(f"/{package}/edits")
    edit_id = data["id"]
    log.info("Created edit: %s", edit_id)
    return edit_id


async def commit_edit(package: str = DEFAULT_PACKAGE, edit_id: str = "") -> dict:
    """Commit (publish) an edit."""
    data = await _post(f"/{package}/edits/{edit_id}:commit")
    log.info("Committed edit: %s", edit_id)
    return data


async def delete_edit(package: str = DEFAULT_PACKAGE, edit_id: str = "") -> None:
    """Delete (discard) an edit."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(
            f"{PLAY_BASE}/{package}/edits/{edit_id}",
            headers=await _headers(),
        )
        resp.raise_for_status()
    log.info("Deleted edit: %s", edit_id)


# --- Listings (Metadata) ---

async def get_listing(
    locale: str,
    *,
    package: str = DEFAULT_PACKAGE,
    edit_id: str = "",
) -> dict:
    """Get listing for a specific locale.

    Returns {title, shortDescription, fullDescription, video}.
    """
    if not edit_id:
        edit_id = await create_edit(package)

    data = await _get(f"/{package}/edits/{edit_id}/listings/{locale}")
    return {
        "locale": locale,
        "title": data.get("title", ""),
        "short_description": data.get("shortDescription", ""),
        "full_description": data.get("fullDescription", ""),
        "video": data.get("video", ""),
        "_edit_id": edit_id,
    }


async def get_all_listings(
    *,
    package: str = DEFAULT_PACKAGE,
    edit_id: str = "",
) -> dict[str, dict]:
    """Get listings for all locales.

    Returns {locale: {title, shortDescription, fullDescription}}.
    """
    if not edit_id:
        edit_id = await create_edit(package)

    data = await _get(f"/{package}/edits/{edit_id}/listings")
    listings = data.get("listings", [])

    result = {}
    for listing in listings:
        locale = listing.get("language", "unknown")
        result[locale] = {
            "locale": locale,
            "title": listing.get("title", ""),
            "short_description": listing.get("shortDescription", ""),
            "full_description": listing.get("fullDescription", ""),
            "video": listing.get("video", ""),
            "_edit_id": edit_id,
        }

    log.info("Fetched %d Play Store listings", len(result))
    return result


async def update_listing(
    locale: str,
    *,
    title: str | None = None,
    short_description: str | None = None,
    full_description: str | None = None,
    package: str = DEFAULT_PACKAGE,
    edit_id: str = "",
    auto_commit: bool = True,
) -> dict:
    """Update listing for a specific locale.

    Only provided fields are updated. Creates and commits edit automatically.

    Args:
        auto_commit: If True, commits the edit after update.
                    Set False to batch multiple updates in one edit.
    """
    if not edit_id:
        edit_id = await create_edit(package)

    # Get current listing to preserve unchanged fields
    current = await get_listing(locale, package=package, edit_id=edit_id)

    payload = {
        "language": locale,
        "title": title if title is not None else current.get("title", ""),
        "shortDescription": (
            short_description if short_description is not None
            else current.get("short_description", "")
        ),
        "fullDescription": (
            full_description if full_description is not None
            else current.get("full_description", "")
        ),
    }

    result = await _put(f"/{package}/edits/{edit_id}/listings/{locale}", payload)
    log.info("Updated Play Store listing: %s.%s", locale, ", ".join(
        k for k, v in {"title": title, "short_description": short_description,
                       "full_description": full_description}.items() if v is not None
    ))

    if auto_commit:
        await commit_edit(package, edit_id)

    return result


async def get_current_metadata(package: str = DEFAULT_PACKAGE) -> dict[str, dict]:
    """Get all listings — symmetric with app_store.get_current_metadata().

    Returns {locale: {title, short_description, full_description}}.
    Edit is created but NOT committed (read-only operation).
    """
    try:
        edit_id = await create_edit(package)
        listings = await get_all_listings(package=package, edit_id=edit_id)
        # Clean up the read-only edit
        await delete_edit(package, edit_id)
        return listings
    except Exception as e:
        log.error("Failed to fetch Play Store metadata: %s", e)
        return {}
