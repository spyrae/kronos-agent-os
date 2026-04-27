"""EXECUTE node — apply approved changes via APIs.

Supports both iOS (App Store Connect) and Android (Google Play Developer API).
Records baseline metrics before changes (for live apps).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from ..state import ASOState
from ..tools import app_store

log = logging.getLogger("aso.nodes.execute")

# Fields that can be updated via appStoreVersionLocalizations PATCH
LOCALIZATION_FIELDS = {"description", "keywords", "promotionalText", "whatsNew", "marketingUrl", "supportUrl"}

# Fields that need appInfoLocalizations endpoint (different API)
APP_INFO_FIELDS = {"title", "subtitle"}


async def execute(state: ASOState) -> dict:
    """Apply the optimization plan changes."""
    plan = state.get("optimization_plan")
    if not plan:
        log.error("EXECUTE: no plan to execute")
        return {"phase": "execute", "error": "no plan"}

    changes = plan.get("changes", [])
    if not changes:
        log.warning("EXECUTE: plan has no changes")
        return {"phase": "execute", "error": "empty plan"}

    log.info("=== EXECUTE: applying %d changes ===", len(changes))

    metadata = state.get("metadata_ios", {})

    # Collect baseline metrics before applying changes (for live apps)
    baseline = await _collect_baseline(state)

    applied = []
    errors = []

    for change in changes:
        locale = change.get("locale", "en-US")
        field = change.get("field", "")
        proposed = change.get("proposed", "")

        # Find localization ID for this locale
        locale_data = metadata.get(locale, {})
        localization_id = locale_data.get("localization_id")

        if not localization_id:
            # Try partial locale match (e.g., "en" matches "en-US")
            for loc_key, loc_data in metadata.items():
                if loc_key.startswith(locale[:2]):
                    localization_id = loc_data.get("localization_id")
                    locale = loc_key
                    break

        if not localization_id:
            error = f"No localization ID found for {locale}"
            log.error(error)
            errors.append({"change": change, "error": error})
            continue

        # Map field names to API field names
        api_field = _map_field_name(field)

        if api_field in LOCALIZATION_FIELDS:
            try:
                await app_store.update_localization(localization_id, api_field, proposed)
                applied.append({
                    "locale": locale,
                    "field": field,
                    "old_value": change.get("current", ""),
                    "new_value": proposed,
                    "localization_id": localization_id,
                    "timestamp": datetime.now(UTC).isoformat(),
                })
                log.info("Applied: %s.%s → %s", locale, field, proposed[:50])
            except Exception as e:
                error = f"Failed to update {locale}.{field}: {e}"
                log.error(error)
                errors.append({"change": change, "error": str(e)})

        elif api_field in APP_INFO_FIELDS:
            # Title/subtitle updates need appInfoLocalizations endpoint
            # This is a different API path — log for now, implement in Phase 4
            log.warning(
                "Title/subtitle changes require appInfoLocalizations API "
                "(not yet implemented). Skipping: %s.%s",
                locale, field,
            )
            errors.append({
                "change": change,
                "error": "title/subtitle updates not yet supported via API",
            })

        else:
            log.warning("Unknown field: %s", field)
            errors.append({"change": change, "error": f"unknown field: {field}"})

    # --- Android (Google Play) changes ---
    android_changes = [c for c in changes if c.get("platform") == "android"]
    if android_changes:
        try:
            from ..tools import play_store

            edit_id = await play_store.create_edit()
            for change in android_changes:
                locale = change.get("locale", "en-US")
                field = change.get("field", "")
                proposed = change.get("proposed", "")

                field_map = {
                    "title": "title",
                    "short_description": "short_description",
                    "full_description": "full_description",
                    "description": "full_description",
                }
                play_field = field_map.get(field)

                if not play_field:
                    errors.append({"change": change, "error": f"unsupported Play Store field: {field}"})
                    continue

                try:
                    await play_store.update_listing(
                        locale,
                        edit_id=edit_id,
                        auto_commit=False,
                        **{play_field: proposed},
                    )
                    applied.append({
                        "platform": "android",
                        "locale": locale,
                        "field": field,
                        "old_value": change.get("current", ""),
                        "new_value": proposed,
                        "timestamp": datetime.now(UTC).isoformat(),
                    })
                    log.info("Applied (Android): %s.%s → %s", locale, field, proposed[:50])
                except Exception as e:
                    errors.append({"change": change, "error": f"Play Store: {e}"})

            # Commit all Android changes in one edit
            if any(a.get("platform") == "android" for a in applied):
                await play_store.commit_edit(edit_id=edit_id)
                log.info("Play Store edit committed")
            else:
                await play_store.delete_edit(edit_id=edit_id)

        except Exception as e:
            log.warning("Play Store execution skipped: %s", e)
            for change in android_changes:
                errors.append({"change": change, "error": f"Play Store unavailable: {e}"})

    log.info(
        "=== EXECUTE: %d applied, %d errors ===",
        len(applied), len(errors),
    )

    return {
        "phase": "execute",
        "changes_applied": {
            "applied": applied,
            "errors": errors,
            "total": len(changes),
            "success_count": len(applied),
            "error_count": len(errors),
        },
        "baseline_metrics": baseline,
        "error": "; ".join(e["error"] for e in errors) if errors else None,
    }


def _map_field_name(field: str) -> str:
    """Map user-friendly field names to API field names."""
    mapping = {
        "keywords": "keywords",
        "description": "description",
        "promotional_text": "promotionalText",
        "promotionalText": "promotionalText",
        "whats_new": "whatsNew",
        "whatsNew": "whatsNew",
        "marketing_url": "marketingUrl",
        "marketingUrl": "marketingUrl",
        "support_url": "supportUrl",
        "supportUrl": "supportUrl",
        "title": "title",
        "subtitle": "subtitle",
    }
    return mapping.get(field, field)


async def _collect_baseline(state: ASOState) -> dict:
    """Collect current metrics as baseline for comparison."""
    baseline = {
        "timestamp": datetime.now(UTC).isoformat(),
        "keyword_positions": {},
        "ratings": {},
    }

    # Keyword positions snapshot
    rankings = state.get("keyword_rankings", {})
    for key, data in rankings.items():
        if data.get("position"):
            baseline["keyword_positions"][data["keyword"]] = data["position"]

    # Ratings snapshot
    reviews = state.get("reviews_summary", {})
    baseline["ratings"] = {
        "avg_rating": reviews.get("avg_rating"),
        "total_ratings": reviews.get("total_ratings"),
    }

    # Analytics snapshot (if available)
    analytics = state.get("analytics", {})
    if analytics and not analytics.get("_note"):
        baseline["analytics"] = analytics

    return baseline
