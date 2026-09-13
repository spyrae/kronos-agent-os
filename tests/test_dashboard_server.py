import asyncio
import logging

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.server import SHUTDOWN_GRACE_SECONDS, SPAStaticFiles, serve_until_cancelled


def test_spa_static_files_falls_back_to_index_for_client_routes(tmp_path):
    (tmp_path / "index.html").write_text('<div id="root">KAOS UI</div>', encoding="utf-8")
    (tmp_path / "asset.txt").write_text("asset", encoding="utf-8")
    app = FastAPI()
    app.mount("/", SPAStaticFiles(directory=str(tmp_path), html=True), name="ui")
    client = TestClient(app)

    deep_link = client.get("/memory")
    asset = client.get("/asset.txt")

    assert deep_link.status_code == 200
    assert "KAOS UI" in deep_link.text
    assert asset.status_code == 200
    assert asset.text == "asset"


def test_ws_logs_rejects_missing_or_invalid_cookie():
    import pytest
    from starlette.websockets import WebSocketDisconnect

    from dashboard.auth import COOKIE_NAME
    from dashboard.server import create_app

    app = create_app()

    # No session cookie.
    no_cookie = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with no_cookie.websocket_connect("/ws/logs"):
            pass
    assert exc_info.value.code == 4401

    # Bogus session cookie.
    bad_cookie = TestClient(app)
    bad_cookie.cookies.set(COOKIE_NAME, "bogus")
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with bad_cookie.websocket_connect("/ws/logs"):
            pass
    assert exc_info.value.code == 4401


def test_ws_logs_accepts_valid_session_cookie():
    from dashboard.auth import COOKIE_NAME, create_session
    from dashboard.server import create_app
    from dashboard.ws.handlers import log_clients

    client = TestClient(create_app())
    token = create_session()
    client.cookies.set(COOKIE_NAME, token)

    with client.websocket_connect("/ws/logs"):
        assert len(log_clients) == 1
    assert not log_clients


async def test_cancelled_dashboard_leaves_no_lifespan_to_fail_at_loop_teardown(caplog):
    """kronos.app stops services by cancelling their tasks; that must not leave uvicorn half-stopped.

    The traceback never appears at the moment of cancellation. Cancelling serve()
    directly strands uvicorn's lifespan task, and the traceback is logged only when
    asyncio.run cancels leftover tasks at loop close. So the test repeats that
    teardown step itself while still capturing — checking right after the cancel
    would pass against the broken code too.
    """
    # ws="none": the lifespan is what is under test; loading uvicorn's websockets
    # protocol would only add that library's upstream deprecation warnings to the run.
    server = uvicorn.Server(uvicorn.Config(FastAPI(), host="127.0.0.1", port=0, log_level="warning", ws="none"))
    # uvicorn.Config rebuilds logging and drops existing handlers, and the "uvicorn"
    # logger does not propagate — attach to the emitting logger, after the config.
    uvicorn_log = logging.getLogger("uvicorn.error")
    uvicorn_log.addHandler(caplog.handler)
    tasks_before = asyncio.all_tasks()
    try:
        task = asyncio.create_task(serve_until_cancelled(server))
        for _ in range(250):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started, "uvicorn did not start"

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        # Contract with kronos.app: the service task still reads as cancelled.
        assert task.cancelled()

        leftovers = [t for t in asyncio.all_tasks() - tasks_before - {asyncio.current_task()} if not t.done()]
        stranded = sorted(getattr(t.get_coro(), "__qualname__", repr(t)) for t in leftovers)
        # What asyncio.run does at loop close — the moment the traceback used to be logged.
        for leftover in leftovers:
            leftover.cancel()
        await asyncio.gather(*leftovers, return_exceptions=True)
    finally:
        uvicorn_log.removeHandler(caplog.handler)

    assert stranded == [], f"uvicorn left tasks for loop teardown to cancel: {stranded}"
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, "stopping the dashboard logged errors:\n" + "\n".join(errors)


async def test_a_crash_inside_serve_still_reaches_the_caller():
    """A real failure must propagate so systemd's Restart=on-failure can act on it."""

    class CrashingServer:
        should_exit = False

        async def serve(self):
            raise RuntimeError("bind failed")

    with pytest.raises(RuntimeError, match="bind failed"):
        await serve_until_cancelled(CrashingServer())


async def test_run_dashboard_bounds_how_long_a_stop_can_wait(monkeypatch):
    """Graceful exit waits for open connections; unbounded, one hung request blocks the stop."""
    import dashboard.server as server_module

    started = []

    async def capture(server):
        started.append(server)

    monkeypatch.setattr(server_module, "DASHBOARD_PASSWORD", "test-password")
    monkeypatch.setattr(server_module, "serve_until_cancelled", capture)

    await server_module.run_dashboard()

    assert len(started) == 1
    assert started[0].config.timeout_graceful_shutdown == SHUTDOWN_GRACE_SECONDS
    assert SHUTDOWN_GRACE_SECONDS is not None
