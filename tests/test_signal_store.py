import pytest

from kronos.config import settings
from kronos.signals.models import SignalCluster, SignalDigest, SignalItem
from kronos.signals.sources import SignalSource
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


def test_upserts_source_and_inserts_item_idempotently_by_source_key(signal_store):
    source = SignalSource(
        id="reddit_local_llama",
        platform="reddit",
        handle="r/LocalLLaMA",
        categories=("news",),
        tier="core",
        trust="community_high",
    )
    signal_store.upsert_source(source)

    item = SignalItem(
        source_id="reddit_local_llama",
        source_platform="reddit",
        source_item_key="post-1",
        title="New local model",
        text="A new model shipped",
        url="https://reddit.com/r/LocalLLaMA/comments/post-1",
        categories=("news",),
        importance_score=55,
        confidence_score=80,
    )

    first = signal_store.save_item(item)
    second = signal_store.save_item(item)

    assert first.inserted is True
    assert second.inserted is False
    assert second.id == first.id
    assert second.duplicate_of == first.id

    stored = signal_store.get_item(first.id)
    assert stored["title"] == "New local model"
    assert stored["categories"] == ["news"]
    assert stored["raw_payload"] == {}

    stats = signal_store.get_source_quality_stats("reddit_local_llama")[0]
    assert stats["items_seen"] == 2
    assert stats["items_inserted"] == 1
    assert stats["duplicate_count"] == 1
    assert stats["avg_importance"] == 55


def test_deduplicates_by_url_and_content_hash(signal_store):
    first = signal_store.save_item(
        SignalItem(
            source_id="x_openai_devs",
            source_platform="x",
            source_item_key="tweet-1",
            title="API update",
            text="Same announcement",
            url="https://x.com/OpenAIDevs/status/1",
            categories=("news",),
        )
    )
    duplicate_url = signal_store.save_item(
        SignalItem(
            source_id="search_ai_news",
            source_platform="search",
            source_item_key="search-result-1",
            title="API update mirror",
            text="Different snippet",
            url="https://x.com/OpenAIDevs/status/1",
            categories=("news",),
        )
    )
    duplicate_hash = signal_store.save_item(
        SignalItem(
            source_id="reddit_ai_agents",
            source_platform="reddit",
            source_item_key="post-2",
            title="API update",
            text="Same announcement",
            categories=("news",),
        )
    )

    assert first.inserted is True
    assert duplicate_url.inserted is False
    assert duplicate_url.id == first.id
    assert duplicate_hash.inserted is False
    assert duplicate_hash.id == first.id
    assert len(signal_store.list_items(category="news")) == 1


def test_cluster_query_assigns_items(signal_store):
    first = signal_store.save_item(
        SignalItem(
            source_id="reddit_local_llama",
            source_platform="reddit",
            source_item_key="post-1",
            title="Local agent release",
            text="Local agent release details",
            categories=("news",),
            importance_score=60,
            confidence_score=70,
        )
    )
    second = signal_store.save_item(
        SignalItem(
            source_id="x_openai_devs",
            source_platform="x",
            source_item_key="tweet-1",
            title="Agent SDK release",
            text="Agent SDK release details",
            categories=("news",),
            importance_score=80,
            confidence_score=90,
        )
    )

    cluster_id = signal_store.create_cluster(
        SignalCluster(
            category="news",
            title="Agent tooling releases",
            summary="Multiple agent tooling updates",
            item_ids=(first.id, second.id),
            importance_score=75,
            confidence_score=85,
        )
    )

    cluster = signal_store.get_cluster(cluster_id)
    assert cluster["source_ids"] == ["reddit_local_llama", "x_openai_devs"]
    assert cluster["platform_ids"] == ["reddit", "x"]
    assert cluster["evidence_count"] == 2
    assert [item["id"] for item in signal_store.get_cluster_items(cluster_id)] == [first.id, second.id]
    assert signal_store.list_clusters(category="news")[0]["id"] == cluster_id


def test_digest_history_query(signal_store):
    item = signal_store.save_item(
        SignalItem(
            source_id="search_ai_news",
            source_platform="search",
            source_item_key="result-1",
            title="AI news",
            text="AI news details",
            categories=("news",),
        )
    )
    cluster_id = signal_store.create_cluster(
        SignalCluster(category="news", title="AI news cluster", item_ids=(item.id,))
    )

    digest_id = signal_store.save_digest(
        SignalDigest(
            destination="Digest: News",
            title="Daily Digest",
            body="<b>Digest</b>",
            categories=("news",),
            item_ids=(item.id,),
            cluster_ids=(cluster_id,),
        )
    )

    digests = signal_store.list_digests(destination="Digest: News")
    assert digests[0]["id"] == digest_id
    assert digests[0]["categories"] == ["news"]
    assert digests[0]["item_ids"] == [item.id]
    assert digests[0]["cluster_ids"] == [cluster_id]
