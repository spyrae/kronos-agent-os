"""Product/business idea signal detection and rendering helpers."""

from __future__ import annotations

from collections.abc import Sequence

from kronos.signals.models import SignalItem

PAIN_PHRASES = (
    "pain point",
    "pain",
    "pains",
    "problem",
    "annoying",
    "friction",
    "struggle",
    "hard to",
    "takes too long",
    "manual",
    "spreadsheet",
    "боль",
    "проблема",
    "сложно",
    "раздражает",
    "руками",
)
JTBD_PHRASES = (
    "i wish",
    "why is there no",
    "why isn't there",
    "looking for a tool",
    "is there a tool",
    "need a tool",
    "how do i automate",
    "how can i automate",
    "to automate",
    "can someone recommend",
    "есть ли сервис",
    "есть ли инструмент",
    "ищу сервис",
    "ищу инструмент",
    "почему нет",
    "как автоматизировать",
)
OPPORTUNITY_PHRASES = (
    "startup idea",
    "business idea",
    "saas idea",
    "product idea",
    "feature request",
    "launching",
    "launched",
    "mvp",
    "waitlist",
    "идея стартапа",
    "идея продукта",
    "запустили",
)
NOISE_PHRASES = (
    "giveaway",
    "airdrop",
    "coupon",
    "promo code",
    "sponsored",
    "newsletter roundup",
    "top 10 business ideas",
    "top ten business ideas",
    "get rich quick",
    "crypto signal",
)
EXPERT_IDEA_SOURCES = {
    "x_ideabrowser",
    "x_lennysan",
    "x_ycombinator",
    "x_startupideaspod",
    "x_levelsio",
}


def idea_signal_score(item: SignalItem) -> float:
    """Return deterministic confidence that an item is useful for ideas."""
    text = _item_text(item)
    score = 0.0

    if any(phrase in text for phrase in NOISE_PHRASES):
        score -= 55
    if any(phrase in text for phrase in JTBD_PHRASES):
        score += 40
    if any(phrase in text for phrase in PAIN_PHRASES):
        score += 30
    if any(phrase in text for phrase in OPPORTUNITY_PHRASES):
        score += 25
    if item.source_id in EXPERT_IDEA_SOURCES:
        score += 20
    if item.source_platform in {"reddit", "telegram"} and any(
        phrase in text for phrase in (*PAIN_PHRASES, *JTBD_PHRASES)
    ):
        score += 10
    if item.url or item.source_url:
        score += 5

    return max(0.0, min(100.0, score))


def is_idea_signal(item: SignalItem, *, min_score: float = 25.0) -> bool:
    """Return True when the item should enter Product/Business Ideas."""
    return idea_signal_score(item) >= min_score


def product_angle_for_items(items: Sequence[SignalItem]) -> str:
    """Infer a scoped product angle from the cluster text."""
    text = _items_text(items)
    if any(term in text for term in ("travel", "trip", "itinerary", "flight", "hotel", "nomad")):
        return "Travel planning workflow or JourneyBay feature experiment."
    if any(term in text for term in ("cursor", "claude code", "codex", "developer", "ide", "coding")):
        return "Developer workflow/tooling feature or micro-SaaS experiment."
    if any(term in text for term in ("automate", "automation", "workflow", "manual", "руками")):
        return "Automation assistant around a repeated manual workflow."
    if any(term in text for term in ("community", "telegram", "reddit", "discord")):
        return "Community intelligence, moderation, or summarization product angle."
    if any(term in text for term in ("content", "newsletter", "seo", "video", "creator")):
        return "Creator/content ops workflow with measurable time savings."
    return "Discovery experiment: validate the pain with interviews or a lightweight landing page."


def why_now_for_items(items: Sequence[SignalItem], *, can_make_trend_claim: bool) -> str:
    """Return a conservative why-now statement separated from evidence."""
    text = _items_text(items)
    if can_make_trend_claim:
        return "Multiple independent signals make this worth structured discovery now."
    if any(term in text for term in ("launch", "launched", "released", "waitlist", "mvp")):
        return "Recent launch/product activity suggests a testable opportunity, not validated demand yet."
    if any(term in text for term in ("ai", "agent", "llm", "automation")):
        return "AI tooling is changing the cost of solving this workflow; evidence is still early."
    return "Fresh anecdote worth watching; needs corroboration before prioritization."


def caveat_for_items(items: Sequence[SignalItem], *, can_make_trend_claim: bool) -> str:
    """Return a guardrail caveat for product/business ideas."""
    if can_make_trend_claim:
        return "Still requires user interviews, willingness-to-pay checks, and usage data."
    platforms = {item.source_platform for item in items}
    if len(platforms) <= 1:
        return "Anecdotal and platform-concentrated; do not call this market demand yet."
    return "Weak evidence; use as discovery backlog item, not roadmap proof."


def _item_text(item: SignalItem) -> str:
    return f"{item.title} {item.text} {item.normalized_text}".lower()


def _items_text(items: Sequence[SignalItem]) -> str:
    return " ".join(_item_text(item) for item in items)
