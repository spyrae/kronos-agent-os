from kronos.signals.models import SignalItem
from kronos.signals.travel import (
    is_travel_insight,
    journeybay_implication_for_items,
    travel_insight_score,
)


def _item(
    title: str,
    text: str = "",
    *,
    platform: str = "reddit",
    source_id: str = "reddit_travel",
) -> SignalItem:
    return SignalItem(
        source_id=source_id,
        source_platform=platform,
        title=title,
        text=text,
        url="https://example.com/thread",
        categories=("travel_insights",),
    )


def test_travel_pain_point_is_insight():
    item = _item(
        "Itinerary sharing is confusing for group travel",
        "I wish trip planning apps made it easier to collaborate offline.",
    )

    assert is_travel_insight(item) is True
    assert travel_insight_score(item) >= 70


def test_generic_destination_content_is_filtered_out():
    item = _item("Top 10 destinations with the best beaches", "Photo dump from my trip report.")

    assert is_travel_insight(item) is False
    assert travel_insight_score(item) < 30


def test_official_competitor_change_is_allowed():
    item = _item(
        "wanderlog: feature update",
        "New shared itinerary map import workflow launched.",
        platform="competitor",
        source_id="competitor_wanderlog",
    )

    assert is_travel_insight(item) is True


def test_journeybay_implication_detects_booking_import():
    item = _item("Flight reservation import problem", "Manual booking and calendar import is annoying.")

    assert "booking/calendar import" in journeybay_implication_for_items([item])
