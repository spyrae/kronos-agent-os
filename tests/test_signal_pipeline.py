import pytest

from kronos.config import settings
from kronos.signals.fetchers.base import FetchResult
from kronos.signals.models import SignalItem
from kronos.signals.pipeline import run_signal_digest
from kronos.signals.store import SignalStore


@pytest.fixture
def signal_store(tmp_path, monkeypatch):
    db_dir = tmp_path / "agent"
    db_dir.mkdir()
    monkeypatch.setattr(settings, "db_dir", str(db_dir))
    monkeypatch.setattr(settings, "db_path", str(db_dir / "session.db"))

    from kronos import db as _db

    _db._instances.clear()
    return SignalStore()


def _write_sources(tmp_path):
    path = tmp_path / "SOURCES.yaml"
    path.write_text(
        """
sources:
  - id: reddit_local_llama
    platform: reddit
    handle: r/LocalLLaMA
    categories: [news]
    tier: core
    trust: community_high
  - id: x_openai_devs
    platform: x
    handle: "@OpenAIDevs"
    categories: [news]
    tier: core
    trust: official
  - id: x_ideas
    platform: x
    handle: "@ideabrowser"
    categories: [ideas]
    tier: core
    trust: expert
  - id: x_ycombinator
    platform: x
    handle: "@ycombinator"
    categories: [news, jobs, ideas]
    tier: core
    trust: expert
  - id: reddit_travel
    platform: reddit
    handle: r/travel
    categories: [travel_insights]
    tier: candidate
    trust: community_high
""",
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_run_signal_digest_dry_run_persists_news_digest(tmp_path, signal_store):
    sources_path = _write_sources(tmp_path)

    async def fake_fetcher(source, options):
        return FetchResult(
            source=source,
            items=(
                SignalItem(
                    source_id=source.id,
                    source_platform=source.platform,
                    source_item_key=f"{source.id}-1",
                    title="Agent tooling release",
                    text="Agent tooling release makes workflows faster",
                    url=f"https://example.com/{source.id}",
                    categories=("news",),
                ),
            ),
        )

    run = await run_signal_digest(
        "news",
        sources_path=sources_path,
        dry_run=True,
        send=False,
        store=signal_store,
        fetchers={"reddit": fake_fetcher, "x": fake_fetcher},
    )

    assert run.saved_item_count == 3
    assert run.cluster_count == 3
    assert run.sent is False
    assert "Digest: News — Signal Intelligence" in run.rendered.body
    assert "Evidence:" in run.rendered.body

    digest = signal_store.list_digests(destination="Digest: News")[0]
    assert digest["title"].startswith("[dry-run]")
    assert digest["categories"] == ["news"]


@pytest.mark.asyncio
async def test_run_signal_digest_filters_sources_by_category(tmp_path, signal_store):
    sources_path = _write_sources(tmp_path)
    seen = []

    async def fake_fetcher(source, options):
        seen.append(source.id)
        return FetchResult(source=source)

    await run_signal_digest(
        "ideas",
        sources_path=sources_path,
        dry_run=True,
        send=False,
        store=signal_store,
        fetchers={"x": fake_fetcher},
    )

    assert seen == ["x_ideas", "x_ycombinator"]


@pytest.mark.asyncio
async def test_run_signal_digest_applies_job_filter_only_to_jobs(tmp_path, signal_store):
    sources_path = _write_sources(tmp_path)

    async def fake_fetcher(source, options):
        return FetchResult(
            source=source,
            items=(
                SignalItem(
                    source_id=source.id,
                    source_platform=source.platform,
                    source_item_key=f"{source.id}-news",
                    title="Startup batch launches new AI tools",
                    text="Product launches and founder updates, not a hiring post.",
                    categories=source.categories,
                ),
            ),
        )

    news_run = await run_signal_digest(
        "news",
        sources_path=sources_path,
        dry_run=True,
        send=False,
        store=signal_store,
        fetchers={"x": fake_fetcher, "reddit": fake_fetcher},
    )

    assert news_run.saved_item_count == 3


@pytest.mark.asyncio
async def test_run_signal_digest_filters_non_idea_items_for_ideas(tmp_path, signal_store):
    sources_path = _write_sources(tmp_path)

    async def fake_fetcher(source, options):
        return FetchResult(
            source=source,
            items=(
                SignalItem(
                    source_id=source.id,
                    source_platform=source.platform,
                    source_item_key=f"{source.id}-promo",
                    title="Top 10 business ideas",
                    text="Sponsored newsletter roundup with a giveaway.",
                    categories=source.categories,
                ),
                SignalItem(
                    source_id=source.id,
                    source_platform=source.platform,
                    source_item_key=f"{source.id}-idea",
                    title="Looking for a tool to automate user research",
                    text="I wish this manual workflow was easier.",
                    categories=source.categories,
                ),
            ),
        )

    ideas_run = await run_signal_digest(
        "ideas",
        sources_path=sources_path,
        dry_run=True,
        send=False,
        store=signal_store,
        fetchers={"x": fake_fetcher},
    )

    assert ideas_run.saved_item_count == 2
    assert "Product angle:" in ideas_run.rendered.body
    stats = signal_store.get_source_quality_stats("x_ideas")[0]
    assert stats["items_seen"] == 2
    assert stats["selected_count"] == 1
    assert stats["clusters_contributed"] == 1


@pytest.mark.asyncio
async def test_run_signal_digest_filters_travel_noise(tmp_path, signal_store):
    sources_path = _write_sources(tmp_path)

    async def fake_fetcher(source, options):
        return FetchResult(
            source=source,
            items=(
                SignalItem(
                    source_id=source.id,
                    source_platform=source.platform,
                    source_item_key=f"{source.id}-noise",
                    title="Top 10 destinations with best beaches",
                    text="Photo dump from my trip report.",
                    categories=source.categories,
                ),
                SignalItem(
                    source_id=source.id,
                    source_platform=source.platform,
                    source_item_key=f"{source.id}-insight",
                    title="Itinerary sharing is confusing for group travel",
                    text="I wish trip planner apps supported collaboration and offline maps.",
                    categories=source.categories,
                ),
            ),
        )

    travel_run = await run_signal_digest(
        "travel_insights",
        sources_path=sources_path,
        dry_run=True,
        send=False,
        store=signal_store,
        fetchers={"reddit": fake_fetcher},
    )

    assert travel_run.saved_item_count == 1
    assert "JourneyBay implication:" in travel_run.rendered.body
