"""Durable turns in the control room (moat phase 10.4)."""

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

from kronos.config import settings
from kronos.session import SessionStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "agent_name", "dash-turns")
    import kronos.db as _db

    _db._instances.clear()

    from dashboard.server import create_app

    app = create_app()
    # Auth is covered by test_dashboard_auth; here we exercise the endpoints.
    from dashboard import auth

    app.dependency_overrides[auth.verify_token] = lambda: True
    yield TestClient(app)
    _db._instances.clear()


def _store():
    return SessionStore(settings.db_path, agent_name="dash-turns")


async def _turn(store, *, thread_id="chat-3", text="собери отчёт"):
    turn_id = await store.begin_turn(thread_id, text)
    await store.append_turn_messages(
        turn_id=turn_id,
        thread_id=thread_id,
        messages=[
            AIMessage(content="", tool_calls=[{"name": "send_report", "args": {}, "id": "c1"}]),
            ToolMessage(content="ok", tool_call_id="c1"),
        ],
    )
    await store.record_external_effect(key="k1", turn_id=turn_id, tool="send_report", result="delivered")
    return turn_id


def test_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    from dashboard.server import create_app

    unauthenticated = TestClient(create_app())

    assert unauthenticated.get("/api/turns").status_code == 401


def test_list_reports_counts_and_in_flight(client):
    import asyncio

    store = _store()
    turn_id = asyncio.run(_turn(store))

    payload = client.get("/api/turns").json()

    assert payload["total"] == 1
    assert payload["running_turns"] == 1
    assert payload["oldest_running_age_seconds"] is not None
    assert payload["counts"]["running"] == 1
    assert payload["turns"][0]["turn_id"] == turn_id


def test_list_filters_by_status(client):
    import asyncio

    store = _store()
    asyncio.run(_turn(store))

    assert client.get("/api/turns?status=failed").json()["total"] == 0
    assert client.get("/api/turns?status=running").json()["total"] == 1


def test_detail_exposes_journal_and_effects(client):
    import asyncio

    turn_id = asyncio.run(_turn(_store()))

    detail = client.get(f"/api/turns/{turn_id}").json()

    assert detail["thread_id"] == "chat-3"
    assert [row["seq"] for row in detail["journal"]] == [1, 2]
    assert detail["effects"][0]["tool"] == "send_report"


def test_detail_404_for_unknown_turn(client):
    assert client.get("/api/turns/nope").status_code == 404


def test_fork_creates_a_thread_and_keeps_the_original(client):
    import asyncio

    turn_id = asyncio.run(_turn(_store()))

    response = client.post(f"/api/turns/{turn_id}/fork", json={"at_seq": 1, "thread": "experiment"})

    assert response.status_code == 200
    assert response.json()["thread_id"] == "experiment"
    detail = client.get(f"/api/turns/{turn_id}").json()
    assert len(detail["journal"]) == 2, "fork must not modify the source turn"


def test_resume_refuses_a_finished_turn(client):
    import asyncio

    store = _store()
    turn_id = asyncio.run(_turn(store))
    asyncio.run(store.finish_turn(turn_id))

    response = client.post(f"/api/turns/{turn_id}/resume")

    assert response.status_code == 409
    assert "only in-flight" in response.json()["detail"]


def test_resume_404_for_unknown_turn(client):
    assert client.post("/api/turns/nope/resume").status_code == 404


def test_resume_finishes_an_in_flight_turn(client, monkeypatch):
    import asyncio

    turn_id = asyncio.run(_turn(_store()))

    class FakeAgent:
        def __init__(self, *args, **kwargs):
            pass

        async def resume_interrupted_turn(self, turn):
            assert turn["turn_id"] == turn_id
            return "Отчёт собран."

    monkeypatch.setattr("kronos.graph.KronosAgent", FakeAgent)

    response = client.post(f"/api/turns/{turn_id}/resume")

    assert response.status_code == 200
    assert response.json()["answer"] == "Отчёт собран."


def test_resume_failure_is_reported(client, monkeypatch):
    import asyncio

    turn_id = asyncio.run(_turn(_store()))

    class FailingAgent:
        def __init__(self, *args, **kwargs):
            pass

        async def resume_interrupted_turn(self, turn):
            return None

    monkeypatch.setattr("kronos.graph.KronosAgent", FailingAgent)

    response = client.post(f"/api/turns/{turn_id}/resume")

    assert response.status_code == 500
    assert "marked failed" in response.json()["detail"]
