"""JourneyBay App Store / Play Store signal fetchers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from kronos.signals.fetchers.base import FetchError, FetchErrorKind, FetchOptions, FetchResult, source_item
from kronos.signals.sources import SignalSource

MetricsCollector = Callable[[], Mapping[str, Any]]


async def fetch_app_store_source(
    source: SignalSource,
    options: FetchOptions | None = None,
    *,
    collector: MetricsCollector | None = None,
) -> FetchResult:
    """Fetch iOS App Store metrics/reviews as official signal items."""
    opts = options or FetchOptions()
    data = await _collect(collector)
    items = _ios_items(source, data, limit=opts.limit)
    if items:
        return FetchResult(source=source, items=tuple(items))
    error = data.get("ios_error") or data.get("error")
    if error:
        return _single_error(source, FetchErrorKind.SOURCE_UNAVAILABLE, str(error))
    return FetchResult(source=source)


async def fetch_play_store_source(
    source: SignalSource,
    options: FetchOptions | None = None,
    *,
    collector: MetricsCollector | None = None,
) -> FetchResult:
    """Fetch Google Play metrics as official signal items when configured."""
    opts = options or FetchOptions()
    data = await _collect(collector)
    items = _android_items(source, data, limit=opts.limit)
    if items:
        return FetchResult(source=source, items=tuple(items))

    error = str(data.get("android_error") or data.get("error") or "")
    if not error or _is_optional_android_config_gap(error):
        return FetchResult(source=source)
    return _single_error(source, FetchErrorKind.SOURCE_UNAVAILABLE, error)


async def _collect(collector: MetricsCollector | None) -> Mapping[str, Any]:
    if collector is None:
        from kronos.analytics.sources.app_store import collect

        collector = collect
    return await asyncio.to_thread(collector)


def _ios_items(source: SignalSource, data: Mapping[str, Any], *, limit: int) -> list:
    items = []
    rating = data.get("ios_rating")
    reviews_count = data.get("ios_reviews_count")
    version = data.get("ios_version")
    release_notes = str(data.get("ios_release_notes") or "").strip()
    if rating is not None or reviews_count is not None or version:
        text_parts = [
            f"Рейтинг iOS: {rating}" if rating is not None else "",
            f"отзывов: {reviews_count}" if reviews_count is not None else "",
            f"версия: {version}" if version else "",
            f"заметки релиза: {release_notes}" if release_notes else "",
        ]
        items.append(
            source_item(
                source,
                title="JourneyBay в App Store: рейтинг, отзывы и версия",
                text="; ".join(part for part in text_parts if part),
                source_item_key="ios_app_store_metrics",
                raw_payload=dict(data),
                importance_score=80.0,
                confidence_score=90.0,
                evidence_level="official",
            )
        )

    for idx, review in enumerate(_reviews(data.get("ios_recent_reviews"))[: max(0, limit - len(items))], start=1):
        title = str(review.get("title") or "").strip()
        body = str(review.get("body") or "").strip()
        rating_text = f"{review.get('rating')}★" if review.get("rating") is not None else "без оценки"
        items.append(
            source_item(
                source,
                title=f"Отзыв iOS {rating_text}: {title or body[:80] or 'без текста'}",
                text=body or title,
                source_item_key=f"ios_review_{review.get('date') or idx}_{idx}",
                published_at=str(review.get("date") or ""),
                raw_payload=dict(review),
                importance_score=75.0,
                confidence_score=85.0,
                evidence_level="official",
            )
        )
    return items[:limit]


def _android_items(source: SignalSource, data: Mapping[str, Any], *, limit: int) -> list:
    rating = data.get("android_rating")
    reviews_count = data.get("android_reviews_count")
    installs = data.get("android_installs")
    version = data.get("android_version")
    if rating is None and reviews_count is None and installs is None and not version:
        return []
    text_parts = [
        f"рейтинг Android: {rating}" if rating is not None else "",
        f"отзывов: {reviews_count}" if reviews_count is not None else "",
        f"установок: {installs}" if installs is not None else "",
        f"версия: {version}" if version else "",
    ]
    return [
        source_item(
            source,
            title="JourneyBay в Google Play: рейтинг, отзывы и версия",
            text="; ".join(part for part in text_parts if part),
            source_item_key="android_play_store_metrics",
            raw_payload=dict(data),
            importance_score=80.0,
            confidence_score=90.0,
            evidence_level="official",
        )
    ][:limit]


def _reviews(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [review for review in value if isinstance(review, Mapping)]


def _is_optional_android_config_gap(error: str) -> bool:
    lowered = error.lower()
    return (
        "package name" in lowered
        or "android_package" in lowered
        or "google-play-scraper not installed" in lowered
    )


def _single_error(source: SignalSource, kind: FetchErrorKind, message: str) -> FetchResult:
    return FetchResult(source=source, errors=(FetchError(kind=kind, source_id=source.id, message=message),))
