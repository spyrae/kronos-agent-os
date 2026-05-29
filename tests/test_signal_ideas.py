from kronos.signals.ideas import idea_signal_score, is_idea_signal, product_angle_for_items
from kronos.signals.models import SignalItem


def _item(title: str, text: str = "", source_id: str = "reddit_ai_agents") -> SignalItem:
    return SignalItem(
        source_id=source_id,
        source_platform="reddit",
        title=title,
        text=text,
        url="https://example.com/thread",
        categories=("ideas",),
    )


def test_jtbd_pain_point_is_idea_signal():
    item = _item(
        "Looking for a tool to automate customer research",
        "I wish there was a simple workflow that summarizes Reddit pains.",
    )

    assert is_idea_signal(item) is True
    assert idea_signal_score(item) >= 70


def test_expert_startup_idea_source_is_allowed():
    item = _item(
        "Startup idea: AI QA for support teams",
        "A focused MVP could start from one narrow workflow.",
        source_id="x_ideabrowser",
    )

    assert is_idea_signal(item) is True


def test_generic_promo_roundup_is_filtered_out():
    item = _item("Top 10 business ideas", "Sponsored newsletter roundup with a giveaway.")

    assert is_idea_signal(item) is False
    assert idea_signal_score(item) < 25


def test_product_angle_detects_travel_workflow():
    item = _item("Itinerary planning is annoying", "Group trip planning takes too long.")

    assert "Travel planning" in product_angle_for_items([item])
