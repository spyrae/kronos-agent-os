"""Source quality audit and keep/drop recommendations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path

from kronos.signals.models import SignalDigest
from kronos.signals.sources import SignalSource, load_sources
from kronos.signals.store import SignalStore

SOURCE_QUALITY_DESTINATION = "Signal Source Quality"


@dataclass(frozen=True)
class SourceRecommendation:
    """Recommendation for one monitored source."""

    source_id: str
    action: str
    reason: str
    evidence: str
    tier: str
    platform: str


@dataclass(frozen=True)
class SourceQualityAudit:
    """Rendered source quality audit artifact."""

    title: str
    body: str
    recommendations: tuple[SourceRecommendation, ...]
    saved_digest_id: int | None = None


def build_source_quality_audit(
    *,
    store: SignalStore | None = None,
    sources_path: str | Path | None = None,
    dry_run: bool = True,
    save: bool = True,
) -> SourceQualityAudit:
    """Build a source quality audit without mutating source configuration."""
    signal_store = store or SignalStore()
    registry = load_sources(sources_path)
    stats_by_source = {row["source_id"]: row for row in signal_store.get_source_quality_stats()}

    recommendations = tuple(
        _recommend(source, stats_by_source.get(source.id)) for source in registry.sources if source.enabled
    )
    title = "Signal Source Quality Audit"
    body = _render_audit(title, recommendations)
    digest_id = None
    if save:
        digest_id = signal_store.save_digest(
            SignalDigest(
                destination=SOURCE_QUALITY_DESTINATION,
                title=f"[dry-run] {title}" if dry_run else title,
                body=body,
                categories=("source_quality",),
            ),
            count_in_quality=False,
        )
    return SourceQualityAudit(title, body, recommendations, digest_id)


def has_recent_source_quality_audit(
    *,
    store: SignalStore | None = None,
    days: int = 13,
) -> bool:
    """Return True if a non-dry-run source audit was generated recently."""
    signal_store = store or SignalStore()
    digests = [
        digest
        for digest in signal_store.list_digests(destination=SOURCE_QUALITY_DESTINATION, limit=5)
        if not str(digest.get("title") or "").startswith("[dry-run]")
    ]
    if not digests:
        return False
    generated_at = str(digests[0].get("generated_at") or "")
    parsed = _parse_utc(generated_at)
    if parsed is None:
        return False
    return datetime.now(UTC) - parsed < timedelta(days=days)


def _recommend(source: SignalSource, stats: dict | None) -> SourceRecommendation:
    metrics = _metrics(stats)
    seen = metrics["seen"]
    selected = metrics["selected"]
    duplicate_rate = metrics["duplicate_rate"]
    selected_rate = metrics["selected_rate"]
    low_rate = metrics["low_rate"]
    avg_importance = metrics["avg_importance"]
    clusters = metrics["clusters"]
    digests = metrics["digests"]
    errors = metrics["errors"]

    if seen == 0:
        action = "watch"
        reason = "No fetched items yet; keep collecting before changing tier."
    elif seen >= 10 and (
        selected_rate < 0.05 or duplicate_rate >= 0.8 or low_rate >= 0.6 or (errors >= 3 and selected == 0)
    ):
        action = "quarantine"
        reason = "Mostly noise/duplicates/errors; exclude from active collection unless manually needed."
    elif seen >= 5 and (selected_rate < 0.2 or duplicate_rate >= 0.6 or low_rate >= 0.4 or avg_importance < 30):
        action = "demote"
        reason = "Low yield relative to fetched volume; keep as candidate or lower priority."
    elif (
        source.tier == "candidate" and selected >= 3 and clusters >= 2 and avg_importance >= 50 and duplicate_rate < 0.5
    ):
        action = "promote"
        reason = "Candidate produced repeated accepted signals with enough unique contribution."
    elif source.tier == "core" or clusters > 0 or digests > 0:
        action = "keep"
        reason = "Source contributed to clusters/digests or is already a core monitored source."
    else:
        action = "watch"
        reason = "Some activity, but not enough evidence for keep/promote/demote."

    return SourceRecommendation(
        source_id=source.id,
        action=action,
        reason=reason,
        evidence=_evidence(metrics),
        tier=source.tier,
        platform=source.platform,
    )


def _render_audit(title: str, recommendations: tuple[SourceRecommendation, ...]) -> str:
    sections = (
        ("Promote", "promote"),
        ("Keep", "keep"),
        ("Demote", "demote"),
        ("Quarantine", "quarantine"),
        ("Watch / insufficient data", "watch"),
    )
    lines = [
        f"<b>{escape(title)}</b>",
        "<i>Recommendations are evidence-based and do not mutate SOURCES.yaml.</i>",
        "",
    ]

    for section_title, action in sections:
        rows = [rec for rec in recommendations if rec.action == action]
        if not rows:
            continue
        rows.sort(key=_recommendation_rank, reverse=True)
        lines.append(f"<b>{escape(section_title)}</b>")
        for rec in rows[:10]:
            lines.append(
                f"• <b>{escape(rec.source_id)}</b> "
                f"({escape(rec.platform)}, {escape(rec.tier)}) — {escape(rec.reason)}\n"
                f"  <i>{escape(rec.evidence)}</i>"
            )
        if len(rows) > 10:
            lines.append(f"  <i>+{len(rows) - 10} more sources omitted for brevity.</i>")
        lines.append("")

    return "\n".join(lines).strip()


def _metrics(stats: dict | None) -> dict[str, float]:
    stats = stats or {}
    seen = int(stats.get("items_seen") or 0)
    selected = int(stats.get("selected_count") or 0)
    duplicates = int(stats.get("duplicate_count") or 0)
    low_conf = int(stats.get("low_confidence_count") or 0)
    return {
        "seen": seen,
        "inserted": int(stats.get("items_inserted") or 0),
        "duplicates": duplicates,
        "selected": selected,
        "low_conf": low_conf,
        "clusters": int(stats.get("clusters_contributed") or 0),
        "digests": int(stats.get("digests_included") or 0),
        "errors": int(stats.get("fetch_error_count") or 0),
        "avg_importance": float(stats.get("avg_importance") or 0.0),
        "avg_confidence": float(stats.get("avg_confidence") or 0.0),
        "selected_rate": selected / seen if seen else 0.0,
        "duplicate_rate": duplicates / seen if seen else 0.0,
        "low_rate": low_conf / selected if selected else 0.0,
    }


def _evidence(metrics: dict[str, float]) -> str:
    return (
        f"seen={int(metrics['seen'])}, accepted={int(metrics['selected'])}, "
        f"dupes={int(metrics['duplicates'])} ({metrics['duplicate_rate']:.0%}), "
        f"low_conf={int(metrics['low_conf'])} ({metrics['low_rate']:.0%}), "
        f"clusters={int(metrics['clusters'])}, digests={int(metrics['digests'])}, "
        f"avg_imp={metrics['avg_importance']:.1f}, avg_conf={metrics['avg_confidence']:.1f}, "
        f"errors={int(metrics['errors'])}"
    )


def _recommendation_rank(rec: SourceRecommendation) -> tuple[int, str]:
    numbers = {
        "quarantine": 5,
        "promote": 4,
        "demote": 3,
        "keep": 2,
        "watch": 1,
    }
    return (numbers.get(rec.action, 0), rec.source_id)


def _parse_utc(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None
