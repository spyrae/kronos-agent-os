from kronos.observer.bookmarks import (
    BookmarkResult,
    BookmarkStatus,
    NoopBookmarkSink,
    RaindropBookmarkSink,
    normalize_url,
    save_bookmarks,
)
from kronos.observer.capture import record_capture
from kronos.workspace import Workspace


class SavedSink:
    def __init__(self):
        self.urls = []

    def save(self, url, *, metadata=None):
        self.urls.append((url, dict(metadata or {})))
        return BookmarkResult(BookmarkStatus.SAVED, url)


class FailingSink:
    def save(self, url, *, metadata=None):
        raise RuntimeError("sink unavailable")


def test_normalize_url_for_bookmark_deduplication():
    assert normalize_url(" HTTPS://Example.COM/a?b=1#section ") == "https://example.com/a?b=1"
    assert normalize_url("www.Example.com/path.") == "https://www.example.com/path"


def test_noop_sink_returns_not_configured_and_duplicates_are_marked():
    results = save_bookmarks(
        [
            "https://Example.com/a#first",
            "https://example.com/a#second",
            "https://example.com/b",
        ],
        sink=NoopBookmarkSink(),
    )

    assert [result.status for result in results] == [
        BookmarkStatus.NOT_CONFIGURED,
        BookmarkStatus.DUPLICATE,
        BookmarkStatus.NOT_CONFIGURED,
    ]
    assert [result.url for result in results] == [
        "https://example.com/a",
        "https://example.com/a",
        "https://example.com/b",
    ]


def test_raindrop_sink_is_optional_and_non_networked(monkeypatch):
    monkeypatch.delenv("RAINDROP_API_TOKEN", raising=False)
    not_configured = RaindropBookmarkSink().save("https://example.com")
    configured = RaindropBookmarkSink(token="secret-token").save("https://example.com")

    assert not_configured.status == BookmarkStatus.NOT_CONFIGURED
    assert configured.status == BookmarkStatus.FAILED
    assert "secret-token" not in (configured.error or "")


def test_save_bookmarks_passes_metadata_to_sink():
    sink = SavedSink()

    results = save_bookmarks(["https://example.com"], sink=sink, metadata={"message_id": 100})

    assert results[0].status == BookmarkStatus.SAVED
    assert sink.urls == [("https://example.com", {"message_id": 100})]


def test_record_capture_persists_standalone_url_when_sink_not_configured(tmp_path):
    workspace = Workspace(tmp_path)

    task = record_capture(
        "https://Example.com/article#fragment",
        is_voice=False,
        is_dm=True,
        message_id=100,
        timestamp="2026-06-19T08:00:00Z",
        workspace=workspace,
    )

    assert task is not None
    metadata = task["source"]["metadata"]
    assert metadata["source_kind"] == "telegram_link"
    assert metadata["message_id"] == 100
    assert metadata["timestamp"] == "2026-06-19T08:00:00Z"
    assert metadata["bookmarks"] == [
        {"status": "not_configured", "url": "https://example.com/article"}
    ]
    assert (workspace.root / task["inbox_path"]).exists()


def test_failed_bookmark_sink_does_not_block_capture(tmp_path):
    workspace = Workspace(tmp_path)

    task = record_capture(
        "https://example.com/a https://example.com/a#copy",
        is_voice=False,
        is_dm=True,
        workspace=workspace,
        bookmark_sink=FailingSink(),
    )

    assert task is not None
    assert task["source"]["kind"] == "telegram_link"
    bookmarks = task["source"]["metadata"]["bookmarks"]
    assert bookmarks[0]["status"] == "failed"
    assert bookmarks[0]["url"] == "https://example.com/a"
    assert bookmarks[1] == {"status": "duplicate", "url": "https://example.com/a"}
    assert (workspace.root / task["inbox_path"]).exists()
