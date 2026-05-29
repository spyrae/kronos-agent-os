"""JourneyBay owned analytics/SEO signal fetchers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from kronos.signals.fetchers.base import FetchError, FetchErrorKind, FetchOptions, FetchResult, source_item
from kronos.signals.sources import SignalSource

MetricsCollector = Callable[[], Mapping[str, Any]]


async def fetch_analytics_source(
    source: SignalSource,
    options: FetchOptions | None = None,
    *,
    collector: MetricsCollector | None = None,
) -> FetchResult:
    """Fetch owned product analytics as official JB system signals."""
    _ = options or FetchOptions()
    data = await _collect(collector or _posthog_collect)
    if _is_optional_gap(data.get("error")):
        return FetchResult(source=source)
    if data.get("error"):
        return _single_error(source, FetchErrorKind.SOURCE_UNAVAILABLE, str(data["error"]))
    return FetchResult(source=source, items=(_analytics_item(source, data),))


async def fetch_seo_source(
    source: SignalSource,
    options: FetchOptions | None = None,
    *,
    collector: MetricsCollector | None = None,
) -> FetchResult:
    """Fetch SEO/GEO snapshot metrics as official JB system signals."""
    _ = options or FetchOptions()
    data = await _collect(collector or _seo_geo_collect)
    if _is_optional_gap(data.get("error")):
        return FetchResult(source=source)
    if data.get("error"):
        return _single_error(source, FetchErrorKind.SOURCE_UNAVAILABLE, str(data["error"]))
    return FetchResult(source=source, items=(_seo_item(source, data),))


async def _collect(collector: MetricsCollector) -> Mapping[str, Any]:
    return await asyncio.to_thread(collector)


def _posthog_collect() -> Mapping[str, Any]:
    from kronos.analytics.sources.posthog import collect

    return collect()


def _seo_geo_collect() -> Mapping[str, Any]:
    from kronos.analytics.sources.seo_geo import collect

    return collect()


def _analytics_item(source: SignalSource, data: Mapping[str, Any]):
    labels = {
        "dau": "DAU",
        "new_signups_24h": "новые регистрации за 24ч",
        "trips_created_24h": "создано поездок за 24ч",
        "ai_messages_24h": "ИИ-сообщений за 24ч",
        "places_saved_24h": "сохранено мест за 24ч",
        "client_errors_24h": "клиентских ошибок за 24ч",
    }
    text = _format_known_metrics(data, labels)
    return source_item(
        source,
        title="JourneyBay: продуктовая аналитика за 24 часа",
        text=text,
        source_item_key="jb_product_analytics",
        raw_payload=dict(data),
        importance_score=85.0,
        confidence_score=90.0,
        evidence_level="official",
    )


def _seo_item(source: SignalSource, data: Mapping[str, Any]):
    text = "; ".join(f"{_humanize_seo_key(str(key))}: {value}" for key, value in data.items())
    return source_item(
        source,
        title="JourneyBay: SEO/GEO снимок",
        text=text,
        source_item_key="jb_seo_geo_snapshot",
        raw_payload=dict(data),
        importance_score=80.0,
        confidence_score=90.0,
        evidence_level="official",
    )


def _format_known_metrics(data: Mapping[str, Any], labels: Mapping[str, str]) -> str:
    parts = [f"{label}: {data[key]}" for key, label in labels.items() if key in data and data[key] is not None]
    if parts:
        return "; ".join(parts)
    return "; ".join(f"{key}: {value}" for key, value in data.items())


def _humanize_seo_key(key: str) -> str:
    result = key.replace("_gsc_", " GSC ").replace("_geo_", " GEO ")
    result = result.replace("_top10", " топ-10").replace("_top20", " топ-20")
    result = result.replace("_clicks_28d", " клики за 28д")
    result = result.replace("_impressions_28d", " показы за 28д")
    result = result.replace("_citation_rate", " citation rate")
    return result.replace("_", " ")


def _is_optional_gap(error: object) -> bool:
    if not error:
        return False
    lowered = str(error).lower()
    return "not configured" in lowered or "no seo/geo data yet" in lowered or "first weekly run pending" in lowered


def _single_error(source: SignalSource, kind: FetchErrorKind, message: str) -> FetchResult:
    return FetchResult(source=source, errors=(FetchError(kind=kind, source_id=source.id, message=message),))
