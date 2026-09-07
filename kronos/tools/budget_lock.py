"""Serialize budget transactions across local threads and agent processes."""

import fcntl
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_TIMEOUT_SECONDS = 30.0
_thread_lock = threading.Lock()


class BudgetLockError(Exception):
    """The budget transaction could not acquire its lock; no write was started."""


@contextmanager
def budget_transaction(path: str) -> Iterator[str]:
    """Hold a stable sidecar lock and yield the canonical budget path.

    Locking BUDGET.md itself would fail after atomic replacement: another process
    could lock its new inode while this process still owns the old one. The
    sidecar is deliberately never removed. One deadline bounds both lock waits.
    """
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    if not _thread_lock.acquire(timeout=max(0.0, LOCK_TIMEOUT_SECONDS)):
        raise BudgetLockError("budget is busy; retry later")
    handle = None
    acquired = False
    try:
        try:
            canonical = Path(path).resolve()
            canonical.parent.mkdir(parents=True, exist_ok=True)
            handle = canonical.with_name(canonical.name + ".lock").open("a")
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BudgetLockError("budget is busy; retry later") from None
                    time.sleep(min(0.05, remaining))
        except OSError as exc:
            raise BudgetLockError(f"cannot lock budget ({type(exc).__name__})") from exc
        yield str(canonical)
    finally:
        try:
            if handle is not None:
                try:
                    if acquired:
                        fcntl.flock(handle, fcntl.LOCK_UN)
                finally:
                    handle.close()
        finally:
            _thread_lock.release()
