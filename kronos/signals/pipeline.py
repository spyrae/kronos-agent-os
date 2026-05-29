"""Orchestration pipeline for category-specific signal digests."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from kronos.signals.digest import RenderedDigest, render_digest, save_rendered_digest
from kronos.signals.fetchers import FetchOptions, FetchResult, fetch_sources
from kronos.signals.fetchers.runner import Fetcher
from kronos.signals.ideas import idea_signal_score, is_idea_signal
from kronos.signals.jobs import is_job_signal, job_signal_score
from kronos.signals.models import SignalCluster, SignalItem
from kronos.signals.routing import topic_id_for_category
from kronos.signals.scoring import assess_evidence, score_item
from kronos.signals.sources import SignalSource, load_sources
from kronos.signals.store import SignalStore


@dataclass(frozen=True)
class SignalDigestRun:
    """Result of a category digest pipeline run."""

    category: str
    rendered: RenderedDigest
    fetch_results: tuple[FetchResult, ...]
    saved_item_count: int
    cluster_count: int
    digest_id: int
    sent: bool


async def run_signal_digest(
    category: str,
    *,
    sources_path: str | Path | None = None,
    dry_run: bool = False,
    send: bool = True,
    topic_id: int | None = None,
    store: SignalStore | None = None,
    fetchers: dict[str, Fetcher] | None = None,
    source_limit: int | None = None,
    fetch_limit: int = 8,
) -> SignalDigestRun:
    """Fetch, score, cluster, render and optionally send one category digest."""
    registry = load_sources(sources_path)
    sources = registry.active(categories=(category,))
    if source_limit is not None:
        sources = sources[:source_limit]

    signal_store = store or SignalStore()
    for source in sources:
        signal_store.upsert_source(source)

    fetch_results = await fetch_sources(
        tuple(sources),
        options=FetchOptions(limit=fetch_limit),
        fetchers=fetchers,
    )
    sources_by_id = {source.id: source for source in sources}
    saved_records = _save_scored_items(signal_store, fetch_results, sources_by_id, category=category)
    clusters, items_by_cluster = _create_clusters(signal_store, category, saved_records, sources_by_id)
    rendered = render_digest(category, clusters, items_by_cluster, sources_by_id=sources_by_id)
    digest_id = save_rendered_digest(signal_store, rendered, dry_run=dry_run)

    sent = False
    if send and not dry_run and saved_records:
        from kronos.cron.notify import send_bot_api

        sent = send_bot_api(rendered.body, topic_id=topic_id or topic_id_for_category(category))

    return SignalDigestRun(
        category=category,
        rendered=rendered,
        fetch_results=tuple(fetch_results),
        saved_item_count=len(saved_records),
        cluster_count=len(clusters),
        digest_id=digest_id,
        sent=sent,
    )


def _save_scored_items(
    store: SignalStore,
    results: list[FetchResult],
    sources_by_id: dict[str, SignalSource],
    *,
    category: str,
) -> list[tuple[int, SignalItem]]:
    records: list[tuple[int, SignalItem]] = []
    for result in results:
        source = sources_by_id.get(result.source.id)
        for item in result.items:
            if category == "jobs" and not is_job_signal(item):
                continue
            if category == "ideas" and not is_idea_signal(item):
                continue
            item_score = item.importance_score or score_item(item, source)
            if category == "jobs":
                item_score = max(item_score, job_signal_score(item))
            if category == "ideas":
                item_score = max(item_score, idea_signal_score(item))
            scored = replace(
                item,
                importance_score=item_score,
                confidence_score=item.confidence_score or min(100.0, item_score),
            )
            write_result = store.save_item(scored)
            records.append((write_result.id, scored))
    return records


def _create_clusters(
    store: SignalStore,
    category: str,
    records: list[tuple[int, SignalItem]],
    sources_by_id: dict[str, SignalSource],
) -> tuple[list[dict[str, Any]], dict[int, tuple[SignalItem, ...]]]:
    grouped: dict[str, list[tuple[int, SignalItem]]] = {}
    for item_id, item in records:
        grouped.setdefault(_cluster_key(item), []).append((item_id, item))

    clusters: list[dict[str, Any]] = []
    items_by_cluster: dict[int, tuple[SignalItem, ...]] = {}
    for group_records in grouped.values():
        item_ids = tuple(dict.fromkeys(item_id for item_id, _ in group_records))
        items = tuple(item for _, item in group_records)
        assessment = assess_evidence(items, sources_by_id=sources_by_id)
        cluster = SignalCluster(
            category=category,
            title=_cluster_title(items),
            summary=_cluster_summary(items),
            evidence_level=assessment.level.value,
            item_ids=item_ids,
            source_ids=tuple(sorted({item.source_id for item in items})),
            platform_ids=tuple(sorted({item.source_platform for item in items})),
            evidence_count=assessment.unique_origin_count,
            source_count=assessment.independent_source_count,
            platform_count=assessment.platform_count,
            importance_score=assessment.score,
            confidence_score=assessment.score,
        )
        cluster_id = store.create_cluster(cluster)
        saved_cluster = store.get_cluster(cluster_id)
        if saved_cluster:
            clusters.append(saved_cluster)
            items_by_cluster[cluster_id] = items

    return clusters, items_by_cluster


def _cluster_key(item: SignalItem) -> str:
    url = item.url or item.source_url
    if url:
        normalized_url = re.sub(r"[?#].*$", "", url.lower()).rstrip("/")
        return f"url:{normalized_url.removeprefix('https://www.').removeprefix('http://www.')}"
    text = item.normalized_text or item.text or item.title or item.source_item_key
    tokens = re.findall(r"[a-zа-я0-9]+", text.lower())
    return "text:" + " ".join(tokens[:8])


def _cluster_title(items: tuple[SignalItem, ...]) -> str:
    return next((item.title for item in items if item.title), "Untitled signal")


def _cluster_summary(items: tuple[SignalItem, ...], limit: int = 280) -> str:
    snippets = []
    for item in items[:3]:
        snippet = item.text or item.normalized_text or item.title
        if snippet:
            snippets.append(snippet)
    summary = " / ".join(snippets)
    return summary[:limit].rstrip()
