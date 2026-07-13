from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.server import SPAStaticFiles


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


def test_ws_logs_rejects_missing_or_invalid_token():
    import pytest
    from starlette.websockets import WebSocketDisconnect

    from dashboard.server import create_app

    client = TestClient(create_app())

    for path in ("/ws/logs", "/ws/logs?token=bogus"):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(path):
                pass
        assert exc_info.value.code == 4401


def test_ws_logs_accepts_valid_session_token():
    from dashboard.auth import create_session
    from dashboard.server import create_app
    from dashboard.ws.handlers import log_clients

    client = TestClient(create_app())
    token = create_session()

    with client.websocket_connect(f"/ws/logs?token={token}"):
        assert len(log_clients) == 1
    assert not log_clients
