"""Diff engine — compare snapshots and detect meaningful changes."""

import logging

from kronos.competitors.models import Change, ChangeType, Severity

log = logging.getLogger("kronos.competitors.diff")

# Thresholds
RATING_SIGNIFICANT = 0.1   # report change
RATING_IMPORTANT = 0.3     # escalate to 'important'
REVIEW_SPIKE_PCT = 0.05    # 5% growth = spike


def diff_snapshots(
    competitor_id: str,
    competitor_name: str,
    channel: str,
    prev: dict | None,
    curr: dict,
) -> list[Change]:
    """Compare previous and current snapshots, return list of changes."""
    if prev is None:
        return [Change(
            competitor_id=competitor_id,
            competitor_name=competitor_name,
            channel=channel,
            change_type=ChangeType.NEW_COMPETITOR,
            severity=Severity.INFO,
            summary=f"First snapshot for {competitor_name} ({channel})",
        )]

    changes: list[Change] = []

    # Version update
    if prev.get("version") and curr.get("version") and prev["version"] != curr["version"]:
        changes.append(Change(
            competitor_id=competitor_id,
            competitor_name=competitor_name,
            channel=channel,
            change_type=ChangeType.VERSION_UPDATE,
            severity=Severity.IMPORTANT,
            summary=(
                f"{competitor_name} updated: "
                f"{prev['version']} \u2192 {curr['version']}"
            ),
            details={
                "old_version": prev["version"],
                "new_version": curr["version"],
                "release_notes": curr.get("release_notes", "No notes"),
            },
        ))

    # Rating change
    prev_rating = prev.get("rating", 0.0)
    curr_rating = curr.get("rating", 0.0)
    if prev_rating and curr_rating:
        rating_diff = abs(curr_rating - prev_rating)
        if rating_diff >= RATING_SIGNIFICANT:
            direction = "\u2191" if curr_rating > prev_rating else "\u2193"
            severity = Severity.IMPORTANT if rating_diff >= RATING_IMPORTANT else Severity.INFO
            changes.append(Change(
                competitor_id=competitor_id,
                competitor_name=competitor_name,
                channel=channel,
                change_type=ChangeType.RATING_CHANGE,
                severity=severity,
                summary=(
                    f"{competitor_name} rating {direction}: "
                    f"{prev_rating:.1f} \u2192 {curr_rating:.1f}"
                ),
                details={
                    "old_rating": prev_rating,
                    "new_rating": curr_rating,
                    "diff": round(curr_rating - prev_rating, 2),
                },
            ))

    # Review count spike
    prev_count = prev.get("rating_count", 0)
    curr_count = curr.get("rating_count", 0)
    if prev_count and curr_count > prev_count:
        review_diff = curr_count - prev_count
        if review_diff > prev_count * REVIEW_SPIKE_PCT:
            changes.append(Change(
                competitor_id=competitor_id,
                competitor_name=competitor_name,
                channel=channel,
                change_type=ChangeType.REVIEW_SPIKE,
                severity=Severity.INFO,
                summary=f"{competitor_name}: +{review_diff} new reviews",
                details={
                    "old_count": prev_count,
                    "new_count": curr_count,
                    "diff": review_diff,
                },
            ))

    # Description change (messaging pivot signal)
    prev_desc = prev.get("description", "")
    curr_desc = curr.get("description", "")
    if prev_desc and curr_desc and prev_desc != curr_desc:
        changes.append(Change(
            competitor_id=competitor_id,
            competitor_name=competitor_name,
            channel=channel,
            change_type=ChangeType.DESCRIPTION_CHANGE,
            severity=Severity.INFO,
            summary=f"{competitor_name} changed App Store description",
            details={
                "old_description": prev_desc[:200],
                "new_description": curr_desc[:200],
            },
        ))

    # Price change
    prev_price = prev.get("price", 0.0)
    curr_price = curr.get("price", 0.0)
    if prev_price != curr_price and (prev_price or curr_price):
        changes.append(Change(
            competitor_id=competitor_id,
            competitor_name=competitor_name,
            channel=channel,
            change_type=ChangeType.PRICING_CHANGE,
            severity=Severity.CRITICAL,
            summary=(
                f"{competitor_name} price change: "
                f"${prev_price} \u2192 ${curr_price}"
            ),
            details={
                "old_price": prev_price,
                "new_price": curr_price,
            },
        ))

    return changes
