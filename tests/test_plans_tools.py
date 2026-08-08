"""What the agent can declare, and what it is told when it declares it wrong.

A condition is refused here, in the turn that wrote it, with the reason
attached — an agent that reads "op must be one of below, above" fixes it now.
The alternative is a plan that looks fine and quietly never wakes.
"""

import json

import pytest

from kronos import plans
from kronos.audit import reset_tool_audit_context, set_tool_audit_context
from kronos.config import settings
from kronos.tools.plans_tools import PLAN_TOOLS, plan_add_step, plan_cancel, plan_start, plan_status

AGENT = "kronos"


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "agent_name", AGENT)
    monkeypatch.setattr("kronos.security.egress.check_url", lambda url, tool="": None)
    import kronos.db as _db

    _db._instances.clear()
    token = set_tool_audit_context(session_id="77", thread_id="77:3", agent=AGENT)
    yield
    reset_tool_audit_context(token)
    _db._instances.clear()


def _start(goal="найти жильё на Бали", step="поищи на airbnb") -> int:
    plan_start.invoke({"goal": goal, "first_step": step})
    return plans.list_plans(AGENT)[0]["id"]


# --- declaring ----------------------------------------------------------------


def test_starting_a_plan_records_where_to_report_back():
    plan_id = _start()

    plan = plans.get_plan(plan_id)
    assert plan["goal"] == "найти жильё на Бали"
    assert plan["chat_id"] == 77
    assert plan["topic_id"] == 3, "a plan started in a topic must answer in that topic"
    assert len(plans.steps_of(plan_id)) == 1


def test_starting_a_plan_tells_the_agent_how_to_continue():
    out = plan_start.invoke({"goal": "цель", "first_step": "шаг"})

    assert "plan_add_step" in out
    assert "plan_id=" in out


def test_a_plan_without_a_goal_is_refused():
    assert plan_start.invoke({"goal": " ", "first_step": "шаг"}).startswith("[ERROR]")


def test_a_step_can_wait_for_a_price_to_fall():
    plan_id = _start()
    wait = json.dumps(
        {
            "kind": "page_number",
            "url": "https://tokopedia.test/rog-ally",
            "pattern": r"Rp ([\d.]+)",
            "op": "below",
            "value": 9_000_000,
        }
    )

    out = plan_add_step.invoke({"plan_id": plan_id, "task": "скажи мне", "wait": wait, "notify": True})

    step = plans.steps_of(plan_id)[-1]
    assert "ждёт" in out
    assert step["state"] == plans.STEP_WAITING
    assert step["notify"] == 1
    assert plans.wait_spec(step)["value"] == 9_000_000


def test_a_page_condition_gets_a_sane_check_interval_even_if_none_was_given():
    plan_id = _start()
    plan_add_step.invoke(
        {
            "plan_id": plan_id,
            "task": "смотри",
            "wait": json.dumps({"kind": "page_matches", "url": "https://x.test/a", "pattern": "in stock"}),
        }
    )

    assert plans.wait_spec(plans.steps_of(plan_id)[-1])["every_seconds"] >= 300


def test_steps_can_fan_in_on_several_earlier_steps():
    plan_id = _start()
    second = plans.add_step(plan_id, "спроси второго")
    first = plans.steps_of(plan_id)[0]["id"]

    plan_add_step.invoke({"plan_id": plan_id, "task": "сравни", "after_steps": f"{first}, {second}"})

    assert plans.dependency_ids(plans.steps_of(plan_id)[-1]) == [first, second]


# --- refusals the agent can act on --------------------------------------------


def test_a_condition_that_is_not_json_says_so():
    plan_id = _start()

    out = plan_add_step.invoke({"plan_id": plan_id, "task": "x", "wait": "через неделю"})

    assert out.startswith("[ERROR]")
    assert "JSON" in out


def test_an_unknown_condition_lists_the_ones_that_exist():
    plan_id = _start()

    out = plan_add_step.invoke({"plan_id": plan_id, "task": "x", "wait": json.dumps({"kind": "vibes"})})

    assert "page_number" in out, "the error should be enough to fix the call"


def test_a_number_condition_without_a_direction_is_refused():
    plan_id = _start()
    wait = json.dumps({"kind": "page_number", "url": "https://x.test/a", "pattern": r"([\d]+)", "value": 1})

    out = plan_add_step.invoke({"plan_id": plan_id, "task": "x", "wait": wait})

    assert "op" in out


def test_a_host_the_policy_forbids_is_refused_at_declaration(monkeypatch):
    def blocked(url, tool=""):
        raise RuntimeError("not on the egress allowlist")

    monkeypatch.setattr("kronos.security.egress.check_url", blocked)
    plan_id = _start()
    wait = json.dumps({"kind": "page_matches", "url": "https://blocked.test/a", "pattern": "x"})

    out = plan_add_step.invoke({"plan_id": plan_id, "task": "x", "wait": wait})

    assert out.startswith("[ERROR]")
    assert "allowlist" in out


def test_a_bad_dependency_list_is_refused():
    plan_id = _start()

    out = plan_add_step.invoke({"plan_id": plan_id, "task": "x", "after_steps": "первый шаг"})

    assert out.startswith("[ERROR]")
    assert "after_steps" in out


def test_adding_to_a_plan_that_does_not_exist_says_so():
    assert plan_add_step.invoke({"plan_id": 999, "task": "x"}).startswith("[ERROR]")


# --- reading and stopping -----------------------------------------------------


def test_status_shows_what_each_step_is_waiting_for():
    plan_id = _start()
    plan_add_step.invoke(
        {"plan_id": plan_id, "task": "потом", "title": "после просмотра", "wait": json.dumps({"kind": "manual"})}
    )

    out = plan_status.invoke({})

    assert f"План #{plan_id}" in out
    assert "после просмотра" in out
    assert "resume" in out or "возоб" in out or "ждёт" in out


def test_status_of_one_plan_includes_its_step_results():
    plan_id = _start()
    plans.finish_step(plans.steps_of(plan_id)[0]["id"], "нашёл три варианта")

    assert "нашёл три варианта" in plan_status.invoke({"plan_id": plan_id})


def test_status_with_no_plans_says_so():
    assert "нет" in plan_status.invoke({}).lower()


def test_status_of_a_missing_plan_is_an_error():
    assert plan_status.invoke({"plan_id": 4242}).startswith("[ERROR]")


def test_cancelling_stops_the_plan_and_is_not_repeatable():
    plan_id = _start()

    assert "остановлен" in plan_cancel.invoke({"plan_id": plan_id, "reason": "передумал"})
    assert plans.get_plan(plan_id)["state"] == plans.PLAN_CANCELLED
    assert plan_cancel.invoke({"plan_id": plan_id}).startswith("[ERROR]")


# --- how the runtime must treat these ----------------------------------------


def test_declaring_a_plan_counts_as_a_side_effect():
    """Durable resume replays a turn; it must not create the plan a second time."""
    by_name = {tool.name: tool for tool in PLAN_TOOLS}

    for name in ("plan_start", "plan_add_step", "plan_cancel"):
        assert (by_name[name].metadata or {}).get("side_effect") is True, name
    assert not (by_name["plan_status"].metadata or {}).get("side_effect"), "reading is not an effect"


def test_a_plan_started_outside_a_chat_still_works():
    """A plan created from cron has nowhere to report, and that is not an error."""
    token = set_tool_audit_context()  # no chat behind this call
    try:
        out = plan_start.invoke({"goal": "фоновая цель", "first_step": "шаг"})
    finally:
        reset_tool_audit_context(token)

    assert not out.startswith("[ERROR]")
    assert plans.list_plans(AGENT)[0]["chat_id"] == 0
