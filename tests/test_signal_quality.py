import pytest

from kronos.config import settings
from kronos.signals.models import SignalCluster, SignalDigest, SignalItem
from kronos.signals.quality import build_source_quality_audit, has_recent_source_quality_audit
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
  - id: candidate_good
    platform: reddit
    handle: r/good
    categories: [ideas]
    tier: candidate
    trust: community_high
  - id: noisy_source
    platform: reddit
    handle: r/noisy
    categories: [news]
    tier: candidate
    trust: noisy
  - id: core_source
    platform: x
    handle: "@core"
    categories: [news]
    tier: core
    trust: expert
""",
        encoding="utf-8",
    )
    return path


def test_source_quality_audit_recommends_promote_and_quarantine(tmp_path, signal_store):
    sources_path = _write_sources(tmp_path)
    ids = []
    for index in range(3):
        result = signal_store.save_item(
            SignalItem(
                source_id="candidate_good",
                source_platform="reddit",
                source_item_key=f"good-{index}",
                title=f"Pain point {index}",
                text="I wish this workflow was easier",
                categories=("ideas",),
                importance_score=70,
                confidence_score=80,
            )
        )
        ids.append(result.id)
    signal_store.record_selection_stats(
        source_id="candidate_good",
        platform="reddit",
        selected_count=3,
    )
    signal_store.create_cluster(SignalCluster(category="ideas", title="Cluster 1", item_ids=tuple(ids[:2])))
    signal_store.create_cluster(SignalCluster(category="ideas", title="Cluster 2", item_ids=tuple(ids[1:])))
    signal_store.record_fetch_stats(source_id="noisy_source", platform="reddit", item_count=20)

    audit = build_source_quality_audit(
        store=signal_store,
        sources_path=sources_path,
        dry_run=True,
        save=False,
    )

    actions = {rec.source_id: rec.action for rec in audit.recommendations}
    assert actions["candidate_good"] == "promote"
    assert actions["noisy_source"] == "quarantine"
    assert "seen=20" in audit.body
    assert "accepted=3" in audit.body


def test_source_quality_dry_run_save_does_not_count_as_recent(tmp_path, signal_store):
    sources_path = _write_sources(tmp_path)

    build_source_quality_audit(
        store=signal_store,
        sources_path=sources_path,
        dry_run=True,
        save=True,
    )
    assert has_recent_source_quality_audit(store=signal_store) is False

    signal_store.save_digest(
        SignalDigest(
            destination="Signal Source Quality",
            title="Signal Source Quality Audit",
            body="ok",
            categories=("source_quality",),
        ),
        count_in_quality=False,
    )
    assert has_recent_source_quality_audit(store=signal_store) is True
