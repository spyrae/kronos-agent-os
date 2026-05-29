import json

import pytest

from kronos.config import settings
from kronos.signals.fetchers.base import FetchResult
from kronos.signals.models import SignalItem
from kronos.signals.store import SignalStore
from kronos.signals.verification import run_signal_dry_run


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
  - id: reddit_ai_agents
    platform: reddit
    handle: r/AI_Agents
    categories: [news]
    tier: core
    trust: community_high
  - id: x_openai_devs
    platform: x
    handle: "@OpenAIDevs"
    categories: [news]
    tier: core
    trust: official
""",
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_signal_dry_run_artifact_contains_counts(tmp_path, signal_store):
    sources_path = _write_sources(tmp_path)

    async def fake_fetcher(source, options):
        return FetchResult(
            source=source,
            items=(
                SignalItem(
                    source_id=source.id,
                    source_platform=source.platform,
                    source_item_key=f"{source.id}-1",
                    title=f"{source.id} agent release",
                    text=f"{source.id} agent release details",
                    categories=("news",),
                    importance_score=70,
                    confidence_score=80,
                ),
            ),
        )

    output = tmp_path / "dry-run.json"
    artifact = await run_signal_dry_run(
        "news",
        sources_path=sources_path,
        output_path=output,
        store=signal_store,
        fetchers={"reddit": fake_fetcher, "x": fake_fetcher},
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert artifact.source_counts == {"reddit_ai_agents": 1, "x_openai_devs": 1}
    assert payload["source_counts"] == artifact.source_counts
    assert payload["saved_item_count"] == 2
    assert payload["cluster_count"] == 2
    assert "confirmed" in payload["evidence_counts"]

    digest = signal_store.list_digests(destination="Digest: News")[0]
    assert digest["title"].startswith("[dry-run]")
