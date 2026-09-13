"""Optional startup and unexpected service exits use distinct lifecycle states."""

import asyncio
import signal
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
async def runtime(monkeypatch):
    from dashboard import server
    from kronos import app, bridge, discord_bridge, policy
    from kronos.cron import delivery

    names = {"bridge", "discord", "scheduler", "dashboard", "delivery"}
    started, cancelled, handlers = set(), set(), {}
    gates = {name: asyncio.Event() for name in names}
    all_started = asyncio.Event()
    behavior = {}

    async def service(name):
        started.add(name)
        if started == names:
            all_started.set()
        try:
            await gates[name].wait()
            mode = behavior.get(name, "return")
            if mode == "raise":
                raise OSError("synthetic service crash")
            if mode == "cancel":
                raise asyncio.CancelledError()
            return True
        finally:
            cancelled.add(name)

    mcp_closed = Mock()

    @asynccontextmanager
    async def mcp():
        try:
            yield []
        finally:
            mcp_closed()

    store = SimpleNamespace(recover_abandoned_turns=AsyncMock())
    agent = SimpleNamespace()
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
        lambda: SimpleNamespace(durable=SimpleNamespace(resume_mode="report", max_resume_attempts=2)),
    )
    monkeypatch.setattr(bridge, "run_bridge", lambda agent: service("bridge"))
    monkeypatch.setattr(discord_bridge, "run_discord", lambda agent: service("discord"))
    original_dashboard = server.run_dashboard
    monkeypatch.setattr(server, "run_dashboard", lambda **kwargs: service("dashboard"))
    monkeypatch.setattr(delivery, "run_delivery_worker", lambda store: service("delivery"))
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb: handlers.setdefault(sig, cb))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: handlers.pop(sig, None))
    tasks = []

    def launch():
        task = asyncio.create_task(app.main())
        tasks.append(task)
        return task

    yield SimpleNamespace(
        launch=launch,
        started=started,
        cancelled=cancelled,
        handlers=handlers,
        gates=gates,
        all_started=all_started,
        behavior=behavior,
        mcp_closed=mcp_closed,
        scheduler=scheduler,
        original_dashboard=original_dashboard,
    )
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("shutdown", ["signal", "cancel"])
async def test_missing_dashboard_password_keeps_real_main_alive(runtime, monkeypatch, shutdown):
    from dashboard import server

    disabled = asyncio.Event()

    async def real_dashboard(**kwargs):
        assert await runtime.original_dashboard(**kwargs) is False
        disabled.set()
        return False

    monkeypatch.setattr(server, "run_dashboard", real_dashboard)
    monkeypatch.setattr(server, "DASHBOARD_PASSWORD", "")
    create_app = Mock(side_effect=AssertionError("must not open an unauthenticated service"))
    monkeypatch.setattr(server, "create_app", create_app)
    task = runtime.launch()
    await asyncio.wait_for(disabled.wait(), 1)
    await asyncio.sleep(0.02)
    assert not task.done()
    assert runtime.started == {"bridge", "discord", "scheduler", "delivery"}
    assert not runtime.cancelled
    create_app.assert_not_called()
    if shutdown == "signal":
        runtime.handlers[signal.SIGTERM]()
        await asyncio.wait_for(task, 1)
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert runtime.cancelled == runtime.started
    runtime.scheduler.stop.assert_called_once()
    runtime.mcp_closed.assert_called_once()
    assert not runtime.handlers


@pytest.mark.parametrize("service", ["bridge", "discord", "scheduler", "dashboard", "delivery"])
@pytest.mark.parametrize("mode", ["return", "raise", "cancel"])
async def test_unexpected_service_exit_is_failure_after_cleanup(runtime, service, mode):
    runtime.behavior[service] = mode
    task = runtime.launch()
    await asyncio.wait_for(runtime.all_started.wait(), 1)
    runtime.gates[service].set()
    error = OSError if mode == "raise" else RuntimeError
    message = "synthetic service crash" if mode == "raise" else f"Service {service} .* unexpectedly"
    with pytest.raises(error, match=message):
        await asyncio.wait_for(task, 1)
    assert runtime.cancelled == runtime.started
    runtime.scheduler.stop.assert_called_once()
    runtime.mcp_closed.assert_called_once()
    assert not runtime.handlers


async def test_signal_with_clean_service_completion_remains_success(runtime):
    task = runtime.launch()
    await asyncio.wait_for(runtime.all_started.wait(), 1)
    runtime.gates["bridge"].set()
    runtime.handlers[signal.SIGTERM]()
    await asyncio.wait_for(task, 1)
    assert runtime.cancelled == runtime.started
    runtime.mcp_closed.assert_called_once()
    assert not runtime.handlers


def test_standalone_dashboard_without_password_exits_failure(monkeypatch, capsys):
    from dashboard import server
    from kronos import cli

    monkeypatch.setattr(cli, "_configure_logging", lambda: None)
    monkeypatch.setattr(server, "DASHBOARD_PASSWORD", "")
    create_app = Mock(side_effect=AssertionError("must not open an unauthenticated service"))
    monkeypatch.setattr(server, "create_app", create_app)
    assert cli.run_dashboard_command() == 1
    assert "Dashboard did not start" in capsys.readouterr().out
    create_app.assert_not_called()


async def test_enabled_dashboard_reports_normal_server_completion(monkeypatch):
    from dashboard import server

    serve = AsyncMock()
    monkeypatch.setattr(server, "DASHBOARD_PASSWORD", "test-only-password")
    monkeypatch.setattr(server, "DASHBOARD_PASSWORD_GENERATED", False)
    monkeypatch.setattr(server, "create_app", lambda **kwargs: object())
    monkeypatch.setattr(server.uvicorn, "Config", lambda *a, **k: object())
    monkeypatch.setattr(server.uvicorn, "Server", lambda config: SimpleNamespace(serve=serve))
    assert await server.run_dashboard() is True
    serve.assert_awaited_once()


@pytest.fixture
def real_dashboard(runtime, monkeypatch):
    """Use a real local uvicorn lifespan with the application's fake dependencies."""
    from fastapi import FastAPI

    from dashboard import server

    entered, exited = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def lifespan(app):
        entered.set()
        try:
            yield
        finally:
            exited.set()

    config = server.uvicorn.Config(
        FastAPI(lifespan=lifespan), host="127.0.0.1", port=0, ws="none", log_level="warning"
    )
    serving = server.uvicorn.Server(config)
    monkeypatch.setattr(server, "run_dashboard", runtime.original_dashboard)
    monkeypatch.setattr(server, "DASHBOARD_PASSWORD", "test-only-password")
    monkeypatch.setattr(server, "DASHBOARD_PASSWORD_GENERATED", False)
    monkeypatch.setattr(server, "create_app", lambda **kwargs: config.app)
    monkeypatch.setattr(server.uvicorn, "Config", lambda *a, **k: config)
    monkeypatch.setattr(server.uvicorn, "Server", lambda config: serving)
    return SimpleNamespace(server=serving, entered=entered, exited=exited)


@pytest.mark.parametrize("shutdown", ["signal", "cancel"])
async def test_real_dashboard_lifespan_closes_with_main(runtime, real_dashboard, shutdown):
    """The optional-service contract must preserve uvicorn's graceful shutdown."""
    tasks_before = asyncio.all_tasks()
    task = runtime.launch()
    await asyncio.wait_for(real_dashboard.entered.wait(), 1)
    async with asyncio.timeout(2):
        while not real_dashboard.server.started:
            await asyncio.sleep(0.01)
    assert not task.done()
    if shutdown == "signal":
        runtime.handlers[signal.SIGTERM]()
        await asyncio.wait_for(task, 2)
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert real_dashboard.exited.is_set()
    assert real_dashboard.server.should_exit
    assert runtime.cancelled == runtime.started
    runtime.scheduler.stop.assert_called_once()
    runtime.mcp_closed.assert_called_once()
    assert not asyncio.all_tasks() - tasks_before
