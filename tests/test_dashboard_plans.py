"""The plans API — the only place a waiting plan is visible.

Resume is the interesting endpoint: it is how a plan that parked itself for the
owner gets going again, and the failure modes are silent ones (releasing a step
of a cancelled plan, releasing a step that was not waiting).
"""

import pytest
from fastapi.testclient import TestClient

from kronos import plans
from kronos.config import settings

AGENT = "kronos"


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "agent_name", AGENT)
    import kronos.db as _db

    _db._instances.clear()
    yield
    _db._instances.clear()


@pytest.fixture
def client():
    from dashboard.server import create_app

    app = create_app()
    # Auth is covered by test_dashboard_auth; here we exercise the endpoints.
    from dashboard import auth

    app.dependency_overrides[auth.verify_token] = lambda: True
    return TestClient(app)


def _plan(goal="найти жильё") -> int:
    return plans.create_plan(agent_name=AGENT, goal=goal, chat_id=1, thread_id="1")


def test_listing_counts_progress_and_waiting(client):
    plan_id = _plan()
    done = plans.add_step(plan_id, "поискал")
    plans.finish_step(done, "нашёл")
    plans.add_step(plan_id, "жду", wait={"kind": "manual"})

    body = client.get("/api/plans").json()

    assert body["plans"][0]["id"] == plan_id
    assert body["plans"][0]["done_count"] == 1
    assert body["plans"][0]["waiting_count"] == 1
    assert body["plans"][0]["step_count"] == 2


def test_finished_plans_are_hidden_until_asked_for(client):
    plan_id = _plan("законченный")
    step = plans.add_step(plan_id, "шаг")
    plans.finish_step(step, "готово")
    plans.settle_plan(plan_id)

    assert client.get("/api/plans").json()["plans"] == []
    assert client.get("/api/plans?all=true").json()["plans"][0]["id"] == plan_id


def test_a_step_says_what_it_waits_for_in_words(client):
    plan_id = _plan()
    plans.add_step(
        plan_id,
        "скажи мне",
        wait={
            "kind": "page_number",
            "url": "https://x.test/a",
            "pattern": r"Rp ([\d.]+)",
            "op": "below",
            "value": 9_000_000,
        },
    )

    step = client.get(f"/api/plans/{plan_id}").json()["steps"][0]

    assert "below" in step["waiting_for"]
    assert "9,000,000" in step["waiting_for"]


def test_a_missing_plan_is_a_404(client):
    assert client.get("/api/plans/4242").status_code == 404


def test_resume_releases_every_waiting_step(client):
    plan_id = _plan()
    parked = plans.add_step(plan_id, "продолжай", wait={"kind": "manual"})

    body = client.post(f"/api/plans/{plan_id}/resume").json()

    assert body["released"] == [parked]
    assert plans.get_step(parked)["state"] == plans.STEP_PENDING


def test_resume_can_target_one_step(client):
    plan_id = _plan()
    first = plans.add_step(plan_id, "один", wait={"kind": "manual"})
    second = plans.add_step(plan_id, "два", wait={"kind": "manual"})

    client.post(f"/api/plans/{plan_id}/resume?step={second}")

    assert plans.get_step(first)["state"] == plans.STEP_WAITING
    assert plans.get_step(second)["state"] == plans.STEP_PENDING


def test_resume_with_nothing_waiting_is_a_404(client):
    plan_id = _plan()
    plans.add_step(plan_id, "уже в очереди")

    assert client.post(f"/api/plans/{plan_id}/resume").status_code == 404


def test_resume_of_a_closed_plan_is_a_conflict(client):
    plan_id = _plan()
    plans.add_step(plan_id, "шаг", wait={"kind": "manual"})
    plans.cancel_plan(plan_id, AGENT)

    assert client.post(f"/api/plans/{plan_id}/resume").status_code == 409


def test_cancelling_returns_the_closed_plan_and_then_404s(client):
    plan_id = _plan()

    assert client.delete(f"/api/plans/{plan_id}").json()["state"] == plans.PLAN_CANCELLED
    assert client.delete(f"/api/plans/{plan_id}").status_code == 404
