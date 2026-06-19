"""Observer/Capture Engine primitives.

The observer package is intentionally local-first and side-effect-free at the
model/state layer. Telegram scanning, capture hooks, and scheduled digests build
on top of these contracts in later tasks.
"""

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

__all__ = [
    "BookmarkCandidate",
    "CaptureDecision",
    "CapturedItem",
    "DailyScopeEntry",
    "DialogSnapshot",
    "ObserverRunResult",
    "ObserverSourceKind",
    "ObserverState",
    "ObserverStateStore",
    "ReplyDebt",
    "classify_capture",
    "extract_urls",
    "is_forced_capture",
    "record_capture",
    "strip_forced_capture_prefix",
]
