"""The transport and shutdown remain live while startup recovery is running."""

import asyncio
import signal
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.parametrize("stop", ["signal", "cancel", "after_recovery"])
async def test_recovery_is_concurrent_and_one_shot_completion_is_not_shutdown(monkeypatch, stop):
    from dashboard import server
    from kronos import app, bridge, discord_bridge, policy
    from kronos.cron import delivery

    started, cancelled = set(), set()
    entered, finish = asyncio.Event(), asyncio.Event()
    handlers = {}

    async def service(name):
        started.add(name)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.add(name)

    async def resume(**kwargs):
        assert kwargs == {"notify": True, "max_attempts": 2}
        entered.set()
        try:
            await finish.wait()
            return 1
        finally:
            cancelled.add("resume")

    @asynccontextmanager
    async def mcp():
        yield []

    store = SimpleNamespace(recover_abandoned_turns=AsyncMock())
    agent = SimpleNamespace(resume_abandoned_turns=resume)
    scheduler = SimpleNamespace(run=lambda: service("scheduler"), stop=Mock())
    for name in ("_activate_policy_or_exit", "_load_swarm_registry_or_exit", "_ensure_data_dirs"):
        monkeypatch.setattr(app, name, lambda: None)
    monkeypatch.setattr(app, "SessionStore", lambda *a, **k: store)
    monkeypatch.setattr(app, "KronosAgent", lambda **k: agent)
    monkeypatch.setattr(app, "managed_mcp_tools", mcp)
    monkeypatch.setattr(app, "Scheduler", lambda: scheduler)
    monkeypatch.setattr(app, "setup_cron_jobs", lambda value: None)
    monkeypatch.setattr(
        policy,
        "get_policy",
        lambda: SimpleNamespace(durable=SimpleNamespace(resume_mode="resume", max_resume_attempts=2)),
    )
    monkeypatch.setattr(bridge, "run_bridge", lambda agent: service("bridge"))
    monkeypatch.setattr(discord_bridge, "run_discord", lambda agent: service("discord"))
    monkeypatch.setattr(server, "run_dashboard", lambda **kwargs: service("dashboard"))

    async def worker(value):
        assert value is store
        await service("delivery")

    monkeypatch.setattr(delivery, "run_delivery_worker", worker)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb: handlers.setdefault(sig, cb))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: handlers.pop(sig, None))
    task = asyncio.create_task(app.main())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert started == {"bridge", "discord", "scheduler", "dashboard", "delivery"}
        assert agent._durable_recovery_checked
        store.recover_abandoned_turns.assert_not_awaited()
        if stop == "after_recovery":
            finish.set()
            await asyncio.sleep(0.02)
            assert not task.done()
            assert not (cancelled - {"resume"})
        if stop == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            handlers[signal.SIGTERM]()
            await asyncio.wait_for(task, 1)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert cancelled == started | {"resume"}
    assert scheduler.stop.called
    assert not handlers
