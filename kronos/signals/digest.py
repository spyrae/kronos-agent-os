"""Evidence-aware Telegram digest rendering for Signal Intelligence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from typing import Any

from kronos.signals.models import SignalDigest, SignalItem
from kronos.signals.routing import DigestRoute, route_for_category
from kronos.signals.scoring import EvidenceLevel, assess_evidence, sanitize_trend_language
from kronos.signals.sources import SignalSource
from kronos.signals.store import SignalStore

TELEGRAM_SAFE_MAX_CHARS = 3900


@dataclass(frozen=True)
class RenderedDigest:
    """Telegram-ready digest artifact."""

    route: DigestRoute
    title: str
    body: str
    categories: tuple[str, ...]
    cluster_ids: tuple[int, ...]
    item_ids: tuple[int, ...]


def render_digest(
    category: str,
    clusters: Sequence[Mapping[str, Any]],
    items_by_cluster: Mapping[int, Sequence[SignalItem]],
    *,
    sources_by_id: Mapping[str, SignalSource] | None = None,
    max_chars: int = TELEGRAM_SAFE_MAX_CHARS,
) -> RenderedDigest:
    """Render scored clusters into a Telegram HTML digest."""
    route = route_for_category(category)
    source_map = dict(sources_by_id or {})
    selected_clusters = [cluster for cluster in clusters if _cluster_category(cluster) == route.category]
    title = f"{route.destination} — Signal Intelligence"

    lines = [f"<b>{escape(title)}</b>", ""]
    if not selected_clusters:
        lines.append("<i>No high-signal items for this window.</i>")
        return RenderedDigest(route, title, "\n".join(lines), (route.category,), (), ())

    sections = _group_clusters(selected_clusters, items_by_cluster, source_map)
    for section_title, rows in sections:
        if not rows:
            continue
        lines.append(f"<b>{escape(section_title)}</b>")
        lines.extend(rows)
        lines.append("")

    cluster_ids = tuple(int(cluster.get("id", 0) or 0) for cluster in selected_clusters if cluster.get("id"))
    item_ids = tuple(
        int(item_id)
        for cluster in selected_clusters
        for item_id in (cluster.get("item_ids") or [])
        if item_id
    )
    body = _truncate_html("\n".join(lines).strip(), max_chars=max_chars)
    return RenderedDigest(route, title, body, (route.category,), cluster_ids, item_ids)


def save_rendered_digest(store: SignalStore, digest: RenderedDigest, *, dry_run: bool = True) -> int:
    """Persist rendered digest metadata; dry-run artifacts are marked in title."""
    title = f"[dry-run] {digest.title}" if dry_run else digest.title
    return store.save_digest(
        SignalDigest(
            destination=digest.route.destination,
            title=title,
            body=digest.body,
            categories=digest.categories,
            item_ids=digest.item_ids,
            cluster_ids=digest.cluster_ids,
        )
    )


def _group_clusters(
    clusters: Sequence[Mapping[str, Any]],
    items_by_cluster: Mapping[int, Sequence[SignalItem]],
    sources_by_id: dict[str, SignalSource],
) -> list[tuple[str, list[str]]]:
    sections = {
        "Confirmed / Official": [],
        "Emerging Signals": [],
        "Anecdotes / Watchlist": [],
    }
    for cluster in clusters:
        cluster_id = int(cluster.get("id", 0) or 0)
        items = tuple(items_by_cluster.get(cluster_id, ()))
        assessment = assess_evidence(items, sources_by_id=sources_by_id)
        rendered = _render_cluster(cluster, items, assessment)
        if assessment.level == EvidenceLevel.CONFIRMED:
            sections["Confirmed / Official"].append(rendered)
        elif assessment.level in {EvidenceLevel.EMERGING_SIGNAL, EvidenceLevel.TREND}:
            sections["Emerging Signals"].append(rendered)
        else:
            sections["Anecdotes / Watchlist"].append(rendered)

    return [(title, rows) for title, rows in sections.items()]


def _render_cluster(cluster: Mapping[str, Any], items: Sequence[SignalItem], assessment) -> str:
    title = str(cluster.get("title") or "Untitled signal")
    summary = str(cluster.get("summary") or "")
    title = sanitize_trend_language(title, assessment)
    summary = sanitize_trend_language(summary, assessment)
    evidence = (
        f"{assessment.independent_source_count} sources / "
        f"{assessment.platform_count} platforms · {assessment.level.value}"
    )
    first_url = next((item.url for item in items if item.url), "")
    link = f' (<a href="{escape(first_url, quote=True)}">source</a>)' if first_url else ""

    parts = [
        f"• <b>{escape(title)}</b>{link}",
        f"  <i>Evidence: {escape(evidence)}</i>",
    ]
    if summary:
        parts.append(f"  {escape(summary)}")
    if not assessment.can_make_trend_claim:
        parts.append("  <i>Guardrail: weak evidence; phrase as observation, not trend.</i>")
    return "\n".join(parts)


def _cluster_category(cluster: Mapping[str, Any]) -> str:
    return str(cluster.get("category") or "").strip().lower()


def _truncate_html(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    suffix = "\n\n<i>…truncated for Telegram length; see stored digest artifact.</i>"
    return text[: max(0, max_chars - len(suffix))].rstrip() + suffix
