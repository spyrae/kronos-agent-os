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
from kronos.observer.commands import (
    ObserverDebtsRun,
    ObserverDigestRun,
    ObserverStatus,
    handle_observer_command,
    ignore_peer,
    mute_peer,
    observer_status,
    render_observer_status,
    run_morning_digest,
    run_reply_debts,
    unignore_peer,
    unmute_peer,
)
from kronos.observer.daily_scope import build_daily_scope, save_daily_scope
from kronos.observer.models import (
    BookmarkCandidate,
    CapturedItem,
    DailyScopeEntry,
    DialogSnapshot,
    ObserverRunResult,
    ObserverSourceKind,
    ReplyDebt,
)
from kronos.observer.reply_debts import detect_reply_debts
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
    "ObserverDebtsRun",
    "ObserverDigestRun",
    "ObserverStatus",
    "NoopBookmarkSink",
    "RaindropBookmarkSink",
    "ReplyDebt",
    "classify_capture",
    "build_daily_scope",
    "detect_reply_debts",
    "extract_urls",
    "handle_observer_command",
    "ignore_peer",
    "is_forced_capture",
    "mute_peer",
    "normalize_url",
    "observer_status",
    "record_capture",
    "render_observer_status",
    "run_morning_digest",
    "run_reply_debts",
    "scan_private_dialogs",
    "save_bookmarks",
    "save_daily_scope",
    "strip_forced_capture_prefix",
    "unignore_peer",
    "unmute_peer",
]
