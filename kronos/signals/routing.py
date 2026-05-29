"""Category-to-destination routing for Signal Intelligence digests."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DigestRoute:
    """Telegram destination for one signal category."""

    category: str
    destination: str
    topic_constant: str
    default_owner_agent: str


CATEGORY_ROUTES = {
    "news": DigestRoute("news", "Digest: News", "TOPIC_DIGEST_NEWS", "kronos"),
    "jobs": DigestRoute("jobs", "Digest: Jobs", "TOPIC_DIGEST_JOBS", "kronos"),
    "ideas": DigestRoute("ideas", "Digest: Product/Business Ideas", "TOPIC_DIGEST_IDEAS", "kronos"),
    "travel_insights": DigestRoute(
        "travel_insights",
        "JB: Travel Insights",
        "TOPIC_JB_TRAVEL_INSIGHTS",
        "kronos",
    ),
    "jb_competitors": DigestRoute(
        "jb_competitors",
        "JB: Competitors Status",
        "TOPIC_JB_COMPETITORS",
        "nexus",
    ),
    "jb_system": DigestRoute("jb_system", "JB: System Status", "TOPIC_JB_SYSTEM", "nexus"),
}


def route_for_category(category: str) -> DigestRoute:
    """Return digest route for a signal category."""
    normalized = category.strip().lower()
    if normalized not in CATEGORY_ROUTES:
        raise ValueError(f"unsupported signal category: {category}")
    return CATEGORY_ROUTES[normalized]


def topic_id_for_category(category: str) -> int:
    """Resolve the configured Telegram topic id for a signal category."""
    route = route_for_category(category)
    from kronos.cron import notify

    return int(getattr(notify, route.topic_constant, 0) or 0)
