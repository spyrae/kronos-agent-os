"""Per-thread serialization for the agent path.

Guards the fix that replaced a single global _agent_lock (which serialized
every chat/topic/DM behind one heavy ReAct cycle) with per-thread locks plus
a bounded global semaphore.
"""

import asyncio

from kronos import bridge


def test_thread_lock_stable_per_thread_and_distinct_across():
    a1 = bridge._thread_lock("chat:1")
    a2 = bridge._thread_lock("chat:1")
    b = bridge._thread_lock("chat:2")
    assert a1 is a2  # same thread → same lock (turns stay ordered)
    assert a1 is not b  # different threads → independent locks


async def _worker(order: list[str], thread_id: str, tag: str):
    async with bridge._thread_lock(thread_id):
        order.append(f"{tag}-enter")
        await asyncio.sleep(0.02)
        order.append(f"{tag}-exit")


async def test_same_thread_is_serialized():
    order: list[str] = []
    await asyncio.gather(
        _worker(order, "same-thread", "A"),
        _worker(order, "same-thread", "B"),
    )
    # one completes fully before the other starts — no interleaving
    assert order in (
        ["A-enter", "A-exit", "B-enter", "B-exit"],
        ["B-enter", "B-exit", "A-enter", "A-exit"],
    )


async def test_distinct_threads_run_concurrently():
    order: list[str] = []
    await asyncio.gather(
        _worker(order, "thread-x", "A"),
        _worker(order, "thread-y", "B"),
    )
    # both enter before either exits — they overlap
    assert set(order[:2]) == {"A-enter", "B-enter"}
