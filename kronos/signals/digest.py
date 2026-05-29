"""Evidence-aware Telegram digest rendering for Signal Intelligence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from typing import Any

from kronos.signals.ideas import caveat_for_items, product_angle_for_items, why_now_for_items
from kronos.signals.models import SignalDigest, SignalItem
from kronos.signals.routing import DigestRoute, route_for_category
from kronos.signals.scoring import EvidenceLevel, assess_evidence, sanitize_trend_language
from kronos.signals.sources import SignalSource
from kronos.signals.store import SignalStore
from kronos.signals.travel import journeybay_implication_for_items, travel_caveat_for_items

TELEGRAM_SAFE_MAX_CHARS = 3900
MAX_IDEA_CLUSTERS = 10
MAX_TRAVEL_CLUSTERS = 10


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
    selected_clusters = _rank_clusters(selected_clusters, items_by_cluster, source_map, category=route.category)
    if route.category == "ideas":
        selected_clusters = selected_clusters[:MAX_IDEA_CLUSTERS]
    if route.category == "travel_insights":
        selected_clusters = selected_clusters[:MAX_TRAVEL_CLUSTERS]
    title = f"{route.destination} — Signal Intelligence"

    lines = [f"<b>{escape(title)}</b>", ""]
    if not selected_clusters:
        lines.append("<i>No high-signal items for this window.</i>")
        return RenderedDigest(route, title, "\n".join(lines), (route.category,), (), ())

    sections = _group_clusters(selected_clusters, items_by_cluster, source_map, category=route.category)
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
        ),
        count_in_quality=not dry_run,
    )


def _group_clusters(
    clusters: Sequence[Mapping[str, Any]],
    items_by_cluster: Mapping[int, Sequence[SignalItem]],
    sources_by_id: dict[str, SignalSource],
    *,
    category: str,
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
        rendered = _render_cluster(cluster, items, assessment, category=category)
        if assessment.level == EvidenceLevel.CONFIRMED:
            sections["Confirmed / Official"].append(rendered)
        elif assessment.level in {EvidenceLevel.EMERGING_SIGNAL, EvidenceLevel.TREND}:
            sections["Emerging Signals"].append(rendered)
        else:
            sections["Anecdotes / Watchlist"].append(rendered)

    return [(title, rows) for title, rows in sections.items()]


def _rank_clusters(
    clusters: Sequence[Mapping[str, Any]],
    items_by_cluster: Mapping[int, Sequence[SignalItem]],
    sources_by_id: dict[str, SignalSource],
    *,
    category: str,
) -> list[Mapping[str, Any]]:
    def sort_key(cluster: Mapping[str, Any]) -> tuple[float, ...]:
        cluster_id = int(cluster.get("id", 0) or 0)
        items = tuple(items_by_cluster.get(cluster_id, ()))
        assessment = assess_evidence(items, sources_by_id=sources_by_id)
        level_rank = {
            EvidenceLevel.CONFIRMED: 5,
            EvidenceLevel.TREND: 4,
            EvidenceLevel.EMERGING_SIGNAL: 3,
            EvidenceLevel.WEAK_SIGNAL: 2,
            EvidenceLevel.ANECDOTE: 1,
        }[assessment.level]
        cluster_score = _float(cluster.get("importance_score")) or _float(cluster.get("confidence_score"))
        category_bonus = 0.0
        if category == "ideas":
            category_bonus = _idea_applicability_score(items)
        elif category == "travel_insights":
            category_bonus = _travel_applicability_score(items)
        return (
            float(level_rank),
            float(assessment.independent_source_count),
            float(assessment.platform_count),
            float(assessment.score),
            category_bonus,
            cluster_score,
        )

    return sorted(clusters, key=sort_key, reverse=True)


def _render_cluster(
    cluster: Mapping[str, Any],
    items: Sequence[SignalItem],
    assessment,
    *,
    category: str,
) -> str:
    if category == "ideas":
        return _render_idea_cluster(cluster, items, assessment)
    if category == "travel_insights":
        return _render_travel_cluster(cluster, items, assessment)

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


def _render_idea_cluster(cluster: Mapping[str, Any], items: Sequence[SignalItem], assessment) -> str:
    title = sanitize_trend_language(str(cluster.get("title") or "Untitled idea"), assessment)
    summary = sanitize_trend_language(str(cluster.get("summary") or ""), assessment)
    evidence = (
        f"{assessment.independent_source_count} sources / "
        f"{assessment.platform_count} platforms · {assessment.level.value}"
    )
    first_url = next((item.url for item in items if item.url), "")
    link = f' (<a href="{escape(first_url, quote=True)}">source</a>)' if first_url else ""
    caveat = caveat_for_items(items, can_make_trend_claim=assessment.can_make_trend_claim)
    why_now = why_now_for_items(items, can_make_trend_claim=assessment.can_make_trend_claim)

    parts = [
        f"• <b>Opportunity:</b> {escape(title)}{link}",
        f"  <i>Evidence: {escape(evidence)}</i>",
    ]
    if summary:
        parts.append(f"  <b>Pain/opportunity:</b> {escape(summary)}")
    parts.extend(
        [
            f"  <b>Product angle:</b> {escape(product_angle_for_items(items))}",
            f"  <b>Why now:</b> {escape(why_now)}",
            f"  <b>Caveat:</b> {escape(caveat)}",
        ]
    )
    if not assessment.can_make_trend_claim:
        parts.append("  <i>Guardrail: treat as discovery input, not validated demand.</i>")
    return "\n".join(parts)


def _render_travel_cluster(cluster: Mapping[str, Any], items: Sequence[SignalItem], assessment) -> str:
    title = sanitize_trend_language(str(cluster.get("title") or "Untitled travel insight"), assessment)
    summary = sanitize_trend_language(str(cluster.get("summary") or ""), assessment)
    evidence = (
        f"{assessment.independent_source_count} sources / "
        f"{assessment.platform_count} platforms · {assessment.level.value}"
    )
    first_url = next((item.url for item in items if item.url), "")
    link = f' (<a href="{escape(first_url, quote=True)}">source</a>)' if first_url else ""
    caveat = travel_caveat_for_items(items, can_make_trend_claim=assessment.can_make_trend_claim)

    parts = [
        f"• <b>Insight:</b> {escape(title)}{link}",
        f"  <i>Evidence: {escape(evidence)}</i>",
    ]
    if summary:
        parts.append(f"  <b>Problem/pain:</b> {escape(summary)}")
    parts.extend(
        [
            f"  <b>JourneyBay implication:</b> {escape(journeybay_implication_for_items(items))}",
            f"  <b>Caveat:</b> {escape(caveat)}",
        ]
    )
    if not assessment.can_make_trend_claim:
        parts.append("  <i>Guardrail: do not describe as a travel market trend yet.</i>")
    return "\n".join(parts)


def _cluster_category(cluster: Mapping[str, Any]) -> str:
    return str(cluster.get("category") or "").strip().lower()


def _idea_applicability_score(items: Sequence[SignalItem]) -> float:
    text = " ".join(f"{item.title} {item.text} {item.normalized_text}".lower() for item in items)
    score = 0.0
    for phrase in ("i wish", "looking for a tool", "pain point", "problem", "автоматизировать", "боль"):
        if phrase in text:
            score += 10
    for phrase in ("travel", "itinerary", "developer", "coding", "workflow", "automation"):
        if phrase in text:
            score += 5
    return score


def _travel_applicability_score(items: Sequence[SignalItem]) -> float:
    text = " ".join(f"{item.title} {item.text} {item.normalized_text}".lower() for item in items)
    score = 0.0
    for phrase in ("itinerary", "trip planner", "booking", "reservation", "maps", "offline", "visa", "budget"):
        if phrase in text:
            score += 8
    for phrase in ("problem", "pain", "wish", "hard to", "can't share", "manual", "confusing"):
        if phrase in text:
            score += 10
    return score


def _float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _truncate_html(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    suffix = "\n\n<i>…truncated for Telegram length; see stored digest artifact.</i>"
    return text[: max(0, max_chars - len(suffix))].rstrip() + suffix
