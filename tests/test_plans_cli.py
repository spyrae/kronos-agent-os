"""`kaos plans` — seeing what is waiting, and being the thing it waits for.

`resume` is the other half of a `manual` condition: the plan parked because only
the owner knows when to carry on. If that command were wrong, those plans would
be unreachable except by editing SQLite.
"""

import json

import pytest

from kronos import plans
from kronos.cli import main
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


def _plan(goal="найти жильё") -> int:
    return plans.create_plan(agent_name=AGENT, goal=goal, chat_id=1, thread_id="1")


def test_list_with_nothing_running_points_at_the_finished_ones(capsys):
    assert main(["plans", "list"]) == 0

    assert "--all" in capsys.readouterr().out


def test_list_shows_progress_and_what_is_waited_for(capsys):
    plan_id = _plan()
    done = plans.add_step(plan_id, "поискал")
    plans.finish_step(done, "нашёл")
    plans.add_step(plan_id, "жду тебя", title="после просмотра", wait={"kind": "manual"})

    assert main(["plans", "list"]) == 0

    out = capsys.readouterr().out
    assert f"#{plan_id}" in out
    assert "1/2 steps" in out
    assert "waits for you to resume it" in out


def test_list_all_includes_what_is_over(capsys):
    finished = _plan("сделано")
    step = plans.add_step(finished, "шаг")
    plans.finish_step(step, "готово")
    plans.settle_plan(finished)

    assert main(["plans", "list"]) == 0
    assert f"#{finished}" not in capsys.readouterr().out

    assert main(["plans", "list", "--all"]) == 0
    assert f"#{finished}" in capsys.readouterr().out


def test_list_json_is_machine_readable(capsys):
    plan_id = _plan()
    plans.add_step(plan_id, "шаг", wait={"kind": "at", "timestamp": 1})

    assert main(["plans", "list", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["id"] == plan_id
    assert payload[0]["waiting"][0]["for"].startswith("until ")


def test_show_prints_the_steps_and_their_results(capsys):
    plan_id = _plan()
    step = plans.add_step(plan_id, "спроси", title="аренда 1")
    plans.finish_step(step, "депозит два месяца")

    assert main(["plans", "show", str(plan_id)]) == 0

    out = capsys.readouterr().out
    assert "аренда 1" in out
    assert "депозит два месяца" in out


def test_show_of_a_missing_plan_fails(capsys):
    assert main(["plans", "show", "4242"]) == 1
    assert "No plan #4242" in capsys.readouterr().out


def test_resume_releases_what_was_waiting_for_the_owner(capsys):
    plan_id = _plan()
    parked = plans.add_step(plan_id, "продолжай", wait={"kind": "manual"})

    assert main(["plans", "resume", str(plan_id)]) == 0

    assert plans.get_step(parked)["state"] == plans.STEP_PENDING
    assert "Released step" in capsys.readouterr().out


def test_resume_can_target_one_step(capsys):
    plan_id = _plan()
    first = plans.add_step(plan_id, "один", wait={"kind": "manual"})
    second = plans.add_step(plan_id, "два", wait={"kind": "manual"})

    assert main(["plans", "resume", str(plan_id), "--step", str(second)]) == 0

    assert plans.get_step(first)["state"] == plans.STEP_WAITING
    assert plans.get_step(second)["state"] == plans.STEP_PENDING


def test_resume_says_when_there_is_nothing_waiting(capsys):
    plan_id = _plan()
    plans.add_step(plan_id, "уже в очереди")

    assert main(["plans", "resume", str(plan_id)]) == 1
    assert "no waiting steps" in capsys.readouterr().out


def test_resume_of_a_closed_plan_does_nothing(capsys):
    plan_id = _plan()
    plans.add_step(plan_id, "шаг", wait={"kind": "manual"})
    plans.cancel_plan(plan_id, AGENT)

    assert main(["plans", "resume", str(plan_id)]) == 1
    assert "cancelled" in capsys.readouterr().out


def test_cancel_stops_it_once(capsys):
    plan_id = _plan()

    assert main(["plans", "cancel", str(plan_id)]) == 0
    assert plans.get_plan(plan_id)["state"] == plans.PLAN_CANCELLED
    assert main(["plans", "cancel", str(plan_id)]) == 1
