"""Scoped memory reset that a delayed writer cannot undo.

A reset advances a generation in swarm.db before it deletes anything. A writer
captures the generation when its turn starts and writes only while it is
unchanged, so facts extracted from a conversation that predates the reset are
dropped instead of written back.

Per-agent stores (Mem0, the local FTS index, the knowledge graph) are guarded
inside this process by a gate: writers share it, a reset holds it exclusively.
Shared stores in swarm.db check the generation inside the writing transaction,
which also covers other agent processes.

A reset covers this agent's own stores and the shared stores. Another agent's
private stores live in another process and are not touched from here.
"""

import asyncio
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from kronos.config import settings

log = logging.getLogger("kronos.memory.reset")

SCOPES = ("facts", "shared", "knowledge_graph", "sessions")
# The layers that background writers fill; resetting them voids writes in flight.
_WRITER_SCOPES = frozenset({"facts", "shared", "knowledge_graph"})
# Mem0 lists 100 memories by default; a fact past that page would be missed.
_MEM0_SCAN_LIMIT = 100_000


class MemoryResetError(RuntimeError):
    """The memory this write belongs to was reset after the write was planned."""


class _WriterGate:
    """Many writers or one reset; a waiting reset is not starved by new writers."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._writers = 0
        self._resetting = False
        self._resets_waiting = 0

    @contextmanager
    def writing(self) -> Iterator[None]:
        with self._condition:
            while self._resetting or self._resets_waiting:
                self._condition.wait()
            self._writers += 1
        try:
            yield
        finally:
            with self._condition:
                self._writers -= 1
                self._condition.notify_all()

    @contextmanager
    def resetting(self) -> Iterator[None]:
        with self._condition:
            self._resets_waiting += 1
            try:
                while self._resetting or self._writers:
                    self._condition.wait()
            finally:
                self._resets_waiting -= 1
            self._resetting = True
        try:
            yield
        finally:
            with self._condition:
                self._resetting = False
                self._condition.notify_all()


_gate = _WriterGate()


def memory_generation(user_id: str) -> str:
    """Generation to capture before work whose result will be written to memory."""
    from kronos.swarm_store import get_swarm

    return get_swarm().memory_generation(user_id)


@contextmanager
def memory_write(user_id: str, generation: str | None) -> Iterator[None]:
    """Write to this agent's memory stores unless a reset has intervened.

    ``generation`` is the value captured when the work started. ``None`` means
    the caller has no earlier point to compare with and only excludes a
    concurrent reset.
    """
    with _gate.writing():
        if generation is not None and memory_generation(user_id) != generation:
            raise MemoryResetError("memory was reset after this write was planned")
        yield


def _fts_path() -> Path:
    return Path(settings.db_dir) / "memory_fts.db"


def _reset_facts(user_id: str) -> dict:
    """Mem0 vectors and the local FTS index, which mirror the same facts."""
    from kronos.memory import fts

    db = fts._get_db()
    if user_id:
        rows = db.read("SELECT id FROM memory_facts WHERE user_id = ?", (user_id,))
        ops = []
        for row in rows:
            ops.append(("DELETE FROM memory_fts WHERE rowid = ?", (row["id"],)))
            ops.append(("DELETE FROM memory_facts WHERE id = ?", (row["id"],)))
        db.write_many(ops)
        deleted = len(rows)
    else:
        deleted = db.read_one("SELECT COUNT(*) AS cnt FROM memory_facts")["cnt"]
        db.write_many([("DELETE FROM memory_fts", ()), ("DELETE FROM memory_facts", ())])

    return {"fts": deleted, "mem0": _reset_mem0(user_id)}


def _reset_mem0(user_id: str) -> str:
    """Delete Mem0 vectors. An unreadable store is an error, never a silent skip."""
    from kronos.memory.store import get_memory

    try:
        memory = get_memory()
    except Exception:
        if not Path(settings.mem0_qdrant_path).exists():
            return "absent"
        raise
    if user_id:
        memory.delete_all(user_id=user_id)
    else:
        memory.reset()
    return "deleted"


def forget_fact(fact_id: int) -> dict:
    """Delete one extracted fact from every layer that mirrors it.

    Mem0 goes first: if its store cannot be changed the fact stays visible in
    the inspector to retry, rather than disappearing from the list while the
    vector copy keeps being recalled.
    """
    from kronos.memory import fts
    from kronos.memory.store import get_memory
    from kronos.swarm_store import get_swarm

    db = fts._get_db()
    row = db.read_one("SELECT user_id, content FROM memory_facts WHERE id = ?", (fact_id,))
    if not row:
        return {"fts": 0, "mem0": 0, "shared": 0}
    user_id, content = row["user_id"], row["content"]

    with _gate.writing():
        vectors = 0
        try:
            memory = get_memory()
        except Exception:
            if Path(settings.mem0_qdrant_path).exists():
                raise
        else:
            stored = memory.get_all(user_id=user_id, limit=_MEM0_SCAN_LIMIT).get("results", [])
            for item in stored:
                if item.get("memory") == content:
                    memory.delete(item["id"])
                    vectors += 1
        db.write_many(
            [
                ("DELETE FROM memory_fts WHERE rowid = ?", (fact_id,)),
                ("DELETE FROM memory_facts WHERE id = ?", (fact_id,)),
            ]
        )
        shared = get_swarm().delete_shared_fact(user_id=user_id, fact=content)
    return {"fts": 1, "mem0": vectors, "shared": shared}


def _reset_shared(user_id: str) -> dict:
    from kronos.swarm_store import get_swarm

    return {"shared_user_facts": get_swarm().delete_shared_facts(user_id=user_id)}


def _reset_graph(user_id: str) -> dict:
    from kronos.memory import knowledge_graph

    return knowledge_graph.delete_rows(user_id=user_id)


async def _reset_sessions(user_id: str) -> dict:
    """Clear stored conversations one by one, each under its own ownership.

    For one user, a conversation someone else also took part in keeps its
    history — it holds their messages too — and loses only this user's rows
    in session search. The count of such threads is reported, not hidden.
    """
    from kronos.session import SessionStore
    from kronos.swarm_store import get_swarm
    from kronos.turn_ownership import own_conversation

    store = SessionStore(settings.db_path, agent_name=settings.agent_name)
    threads = await store.thread_ids(user_id=user_id)
    shared = await store.thread_ids(user_id=user_id, shared=True) if user_id else []
    cleared = 0
    for thread_id in threads:
        async with own_conversation(store.db_path, thread_id):
            cleared += await store.clear(thread_id)
    # This user's search rows everywhere, including shared and trimmed history.
    cleared += get_swarm().delete_session_index(agent_name=settings.agent_name, user_id=user_id)
    return {"threads": len(threads), "shared_threads_kept": len(shared), "rows": cleared}


async def reset_memory(scopes: set[str], *, user_id: str = "") -> dict:
    """Delete the requested layers for one user, or for everyone when ``user_id`` is empty.

    Every layer is attempted. The result names what each one did, and ``ok`` is
    true only if none failed — a partial reset is never reported as a reset.
    """
    unknown = scopes - set(SCOPES)
    if unknown:
        raise ValueError(f"unknown memory scope: {sorted(unknown)}")
    owner = str(user_id or "").strip()

    from kronos.swarm_store import get_swarm

    layers: dict[str, dict] = {}
    errors: dict[str, str] = {}

    def _sync_layers() -> None:
        # Advance first: a writer that planned its write earlier now refuses,
        # including one that is still waiting for the gate. Only when memory
        # writers are affected — clearing sessions alone does not void the
        # facts a turn in flight is about to store.
        if scopes & _WRITER_SCOPES:
            get_swarm().advance_memory_generation(owner)
        with _gate.resetting():
            for scope, action in (
                ("facts", _reset_facts),
                ("shared", _reset_shared),
                ("knowledge_graph", _reset_graph),
            ):
                if scope not in scopes:
                    continue
                try:
                    layers[scope] = action(owner)
                except Exception as error:
                    log.error("Memory reset failed for layer %s: %s", scope, type(error).__name__)
                    errors[scope] = type(error).__name__

    await asyncio.to_thread(_sync_layers)

    if "sessions" in scopes:
        try:
            layers["sessions"] = await _reset_sessions(owner)
        except Exception as error:
            log.error("Memory reset failed for layer sessions: %s", type(error).__name__)
            errors["sessions"] = type(error).__name__

    return {
        "ok": not errors,
        "agent": settings.agent_name,
        "user_id": owner,
        "layers": layers,
        "errors": errors,
    }
