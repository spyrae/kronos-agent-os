"""Observer/Capture Engine primitives.

The observer package is intentionally local-first and side-effect-free at the
model/state layer. Telegram scanning, capture hooks, and scheduled digests build
on top of these contracts in later tasks.
"""

from kronos.observer.bookmarks import (
    BookmarkResult,
    BookmarkSink,
    BookmarkStatus,
    NoopBookmarkSink,
    RaindropBookmarkSink,
    normalize_url,
    save_bookmarks,
)
from kronos.observer.capture import (
    CaptureDecision,
    classify_capture,
    extract_urls,
    is_forced_capture,
    record_capture,
    strip_forced_capture_prefix,
)
from kronos.observer.models import (
    BookmarkCandidate,
    CapturedItem,
    DailyScopeEntry,
    DialogSnapshot,
    ObserverRunResult,
    ObserverSourceKind,
    ReplyDebt,
)
from kronos.observer.state import ObserverState, ObserverStateStore
from kronos.observer.telegram_scan import scan_private_dialogs

__all__ = [
    "BookmarkCandidate",
    "BookmarkResult",
    "BookmarkSink",
    "BookmarkStatus",
    "CaptureDecision",
    "CapturedItem",
    "DailyScopeEntry",
    "DialogSnapshot",
    "ObserverRunResult",
    "ObserverSourceKind",
    "ObserverState",
    "ObserverStateStore",
    "NoopBookmarkSink",
    "RaindropBookmarkSink",
    "ReplyDebt",
    "classify_capture",
    "extract_urls",
    "is_forced_capture",
    "normalize_url",
    "record_capture",
    "scan_private_dialogs",
    "save_bookmarks",
    "strip_forced_capture_prefix",
]
