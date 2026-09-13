"""Single-host execution ownership shared by chat, approvals and recovery."""

import asyncio
import fcntl
import hashlib
import os
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from kronos.effect_state import DurableStateError


class TurnBusyError(DurableStateError):
    """Another executor owns this conversation; no recovery was started."""


@dataclass
class TurnOwnership:
    """Task-local proof that the caller holds the conversation's kernel lock."""

    db_path: Path
    thread_id: str
    _task: asyncio.Task | None = field(repr=False)
    _pid: int = field(repr=False)
    _held: bool = field(default=True, repr=False)

    def assert_held(self, db_path: str, thread_id: str) -> None:
        """Reject a released, cross-task, cross-process or wrong-thread claim."""
        if (
            not self._held
            or self._pid != os.getpid()
            or self._task is not asyncio.current_task()
            or self.db_path != Path(db_path).resolve()
            or self.thread_id != thread_id
        ):
            raise DurableStateError("resume requires current conversation ownership")


@asynccontextmanager
async def own_conversation(db_path: str, thread_id: str, *, wait: bool = True) -> AsyncIterator[TurnOwnership]:
    """Own a conversation across processes, without an expiring lease.

    All executors must share the local SQLite path and this protocol. A lock is
    held until execution unwinds or the process exits, including SIGKILL. Never
    unlink these sidecars: replacing an inode would create two separate locks.
    Locks are per conversation, not turn, to protect read/modify/write history.
    """
    canonical = Path(db_path).resolve()
    directory = canonical.with_name(f".{canonical.name}.turn-locks")
    filename = hashlib.sha256(thread_id.encode()).hexdigest() + ".lock"
    fd: int | None = None
    ownership: TurnOwnership | None = None
    try:
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(directory / filename, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise DurableStateError("conversation lock must be a regular file")
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not wait:
                        raise TurnBusyError("conversation has a live executor; resume refused") from None
                    await asyncio.sleep(0.05)
        except OSError as error:
            raise DurableStateError("cannot acquire conversation ownership") from error
        ownership = TurnOwnership(canonical, thread_id, asyncio.current_task(), os.getpid())
        yield ownership
    finally:
        if ownership is not None:
            ownership._held = False
        if fd is not None:
            # close(), rather than LOCK_UN, also preserves a forked child's
            # inherited lock until that child closes it or execs (CLOEXEC).
            os.close(fd)
