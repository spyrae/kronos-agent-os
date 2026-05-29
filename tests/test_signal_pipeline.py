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

    assert run.saved_item_count == 2
    assert run.cluster_count == 2
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

    assert seen == ["x_ideas"]
