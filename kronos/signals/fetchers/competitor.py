"""JourneyBay competitor signal adapter."""

from __future__ import annotations

from collections.abc import Callable

from kronos.competitors.store import CompetitorStore
from kronos.signals.fetchers.base import FetchOptions, FetchResult, source_item
from kronos.signals.sources import SignalSource

ChangeLoader = Callable[[SignalSource], list[dict]]


async def fetch_competitor_source(
    source: SignalSource,
    *,
    options: FetchOptions | None = None,
    change_loader: ChangeLoader | None = None,
) -> FetchResult:
    """Convert competitor-monitor changes into normalized signal items."""
    opts = options or FetchOptions()
    changes = (change_loader or _load_changes)(source)
    items = tuple(_change_to_item(source, change) for change in changes[: opts.limit])
    return FetchResult(source=source, items=items)


def _load_changes(source: SignalSource) -> list[dict]:
    store = CompetitorStore()
    competitor_id = source.handle
    return [
        change
        for change in store.get_undigested_changes()
        if not competitor_id or change.get("competitor_id") == competitor_id
    ]


def _change_to_item(source: SignalSource, change: dict) -> object:
    change_id = str(change.get("id") or "")
    competitor_id = str(change.get("competitor_id") or source.handle)
    channel = str(change.get("channel") or "")
    summary = str(change.get("summary") or "")
    severity = str(change.get("severity") or "info")
    return source_item(
        source,
        title=f"{competitor_id}: {change.get('change_type') or 'change'}",
        text=summary,
        source_item_key=change_id or f"{competitor_id}:{channel}:{summary}",
        author=competitor_id,
        published_at=str(change.get("detected_at") or ""),
        raw_payload=dict(change),
        importance_score=_severity_score(severity),
        confidence_score=80.0,
        evidence_level="official_observation",
    )


def _severity_score(severity: str) -> float:
    return {
        "critical": 90.0,
        "important": 70.0,
        "info": 45.0,
    }.get(severity, 40.0)
