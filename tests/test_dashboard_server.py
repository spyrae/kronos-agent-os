from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.server import SPAStaticFiles


def test_spa_static_files_falls_back_to_index_for_client_routes(tmp_path):
    (tmp_path / "index.html").write_text("<div id=\"root\">KAOS UI</div>", encoding="utf-8")
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

