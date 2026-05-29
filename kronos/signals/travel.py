"""Travel-domain insight detection and JourneyBay implications."""

from __future__ import annotations

from collections.abc import Sequence

from kronos.signals.models import SignalItem

TRAVEL_TERMS = (
    "travel",
    "trip",
    "itinerary",
    "flight",
    "hotel",
    "booking",
    "visa",
    "passport",
    "maps",
    "route",
    "destination",
    "solo travel",
    "digital nomad",
    "award travel",
    "luggage",
    "packing",
    "journey",
)
TRAVEL_PAIN_TERMS = (
    "pain point",
    "problem",
    "friction",
    "annoying",
    "confusing",
    "hard to",
    "difficult",
    "manual",
    "spreadsheet",
    "lost reservation",
    "delay",
    "cancelled",
    "offline",
    "can't share",
    "cannot share",
    "group planning",
    "budget",
    "split costs",
    "wish",
    "looking for",
)
FEATURE_TERMS = (
    "import",
    "calendar",
    "map",
    "offline",
    "collaborate",
    "share",
    "checklist",
    "budget",
    "visa",
    "notification",
    "ai planner",
    "recommendation",
)
NOISE_TERMS = (
    "best beaches",
    "top 10 destinations",
    "where should i go",
    "photo dump",
    "trip report",
    "cheap flights deal",
    "sale ends",
)
OFFICIAL_TRAVEL_PLATFORMS = {"competitor", "app_store", "play_store"}


def travel_insight_score(item: SignalItem) -> float:
    """Return deterministic confidence that an item is a JB travel insight."""
    text = _item_text(item)
    score = 0.0

    if item.source_platform in OFFICIAL_TRAVEL_PLATFORMS:
        score += 45
    if any(term in text for term in TRAVEL_TERMS):
        score += 25
    if any(term in text for term in TRAVEL_PAIN_TERMS):
        score += 35
    if any(term in text for term in FEATURE_TERMS):
        score += 20
    if item.source_platform in {"reddit", "search"} and any(term in text for term in TRAVEL_PAIN_TERMS):
        score += 10
    if any(term in text for term in NOISE_TERMS):
        score -= 45
    if item.url or item.source_url:
        score += 5

    return max(0.0, min(100.0, score))


def is_travel_insight(item: SignalItem, *, min_score: float = 30.0) -> bool:
    """Return True when the item should enter JB: Travel Insights."""
    return travel_insight_score(item) >= min_score


def journeybay_implication_for_items(items: Sequence[SignalItem]) -> str:
    """Infer an actionable JourneyBay product implication."""
    text = _items_text(items)
    if any(term in text for term in ("group", "share", "collaborate", "can't share", "cannot share")):
        return "Проверить совместные маршруты, права доступа и редактирование поездки несколькими людьми."
    if any(term in text for term in ("flight", "hotel", "booking", "reservation", "calendar", "import")):
        return "Проверить импорт бронирований/календаря, алерты по изменениям и автообновление маршрута."
    if any(term in text for term in ("offline", "maps", "route", "google maps")):
        return "Усилить офлайн-доступ, связку с картами и дневной контекст маршрута."
    if any(term in text for term in ("budget", "split costs", "points", "award travel")):
        return "Проверить планирование с бюджетом/баллами и совместный учёт расходов."
    if any(term in text for term in ("visa", "passport", "nomad", "long-stay", "long stay")):
        return "Добавить гипотезы про чек-листы: визы, документы, лимиты пребывания и ограничения стран."
    if any(term in text for term in ("ai", "planner", "recommendation")):
        return "Сфокусировать AI-планировщик на ограничениях, объяснимости и редактируемых шагах маршрута."
    return "Превратить сигнал в вопрос для исследования, онбординга или планировщика JourneyBay."


def travel_caveat_for_items(items: Sequence[SignalItem], *, can_make_trend_claim: bool) -> str:
    """Return a conservative caveat for travel insights."""
    if can_make_trend_claim:
        return "Сигнал полезный, но перед дорожной картой нужны интервью и подтверждение в поведении пользователей."
    platforms = {item.source_platform for item in items}
    if len(platforms) <= 1:
        return "Единичный/одноплатформенный сигнал: годится для исследования, но не доказывает спрос."
    return "Слабое подтверждение; держать как гипотезу до повторения в отзывах, поддержке или аналитике."


def _item_text(item: SignalItem) -> str:
    return f"{item.title} {item.text} {item.normalized_text}".lower()


def _items_text(items: Sequence[SignalItem]) -> str:
    return " ".join(_item_text(item) for item in items)
