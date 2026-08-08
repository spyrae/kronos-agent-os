"""A plan is mostly a state machine, and its interesting states are the stuck ones.

What these check: a dependency that failed still lets the next step run (one
silent landlord must not mean no answer at all), a retry does not end a
week-long plan, and every way a plan could wait forever has a bound that ends it
with a stated failure instead.
"""

import pytest

from kronos import plans
from kronos.config import settings

AGENT = "kronos"


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    import kronos.db as _db

    _db._instances.clear()
    yield
    _db._instances.clear()


def _plan(goal: str = "find a flat in Bali") -> int:
    return plans.create_plan(agent_name=AGENT, goal=goal, chat_id=1, thread_id="1")


# --- shape --------------------------------------------------------------------


def test_a_plan_holds_its_goal_and_stays_active():
    plan_id = _plan()

    plan = plans.get_plan(plan_id)

    assert plan["goal"] == "find a flat in Bali"
    assert plan["state"] == plans.PLAN_ACTIVE
    assert plan["expires_at"] > plan["created_at"]


def test_a_plan_needs_a_goal():
    with pytest.raises(plans.PlanError, match="needs a goal"):
        plans.create_plan(agent_name=AGENT, goal="   ")


def test_steps_keep_their_order():
    plan_id = _plan()

    first = plans.add_step(plan_id, "search airbnb")
    second = plans.add_step(plan_id, "search booking")

    assert [s["id"] for s in plans.steps_of(plan_id)] == [first, second]
    assert [s["seq"] for s in plans.steps_of(plan_id)] == [1, 2]


def test_a_parked_step_starts_out_waiting():
    plan_id = _plan()

    step_id = plans.add_step(plan_id, "check the price", wait={"kind": "at", "seconds": 3600})

    step = plans.get_step(step_id)
    assert step["state"] == plans.STEP_WAITING
    assert plans.wait_spec(step) == {"kind": "at", "seconds": 3600}


def test_a_step_cannot_depend_on_a_step_of_another_plan():
    other = plans.add_step(_plan("other"), "something")
    plan_id = _plan()

    with pytest.raises(plans.PlanError, match="not part of plan"):
        plans.add_step(plan_id, "compare", depends_on=[other])


def test_steps_are_capped():
    plan_id = _plan()
    for i in range(plans.MAX_STEPS_PER_PLAN):
        plans.add_step(plan_id, f"step {i}")

    with pytest.raises(plans.PlanError, match="already has"):
        plans.add_step(plan_id, "one too many")


def test_a_closed_plan_takes_no_new_steps():
    plan_id = _plan()
    plans.cancel_plan(plan_id, AGENT)

    with pytest.raises(plans.PlanError, match="not active"):
        plans.add_step(plan_id, "too late")


def test_an_unreadable_condition_reads_as_no_condition():
    """Corrupt JSON must not crash the poller for every other plan."""
    plan_id = _plan()
    step_id = plans.add_step(plan_id, "x", wait={"kind": "at"})
    plans._db().write("UPDATE plan_steps SET wait_json = '{oops' WHERE id = ?", (step_id,))

    assert plans.wait_spec(plans.get_step(step_id)) == {}


# --- what is ready to run -----------------------------------------------------


def test_a_fresh_step_is_ready():
    plan_id = _plan()
    step_id = plans.add_step(plan_id, "search airbnb")

    assert [s["id"] for s in plans.ready_steps(AGENT)] == [step_id]


def test_a_step_waits_for_what_it_depends_on():
    plan_id = _plan()
    first = plans.add_step(plan_id, "ask the landlord")
    second = plans.add_step(plan_id, "compare", depends_on=[first])

    assert [s["id"] for s in plans.ready_steps(AGENT)] == [first]

    plans.finish_step(first, "asked")

    assert [s["id"] for s in plans.ready_steps(AGENT)] == [second]


def test_a_failed_dependency_still_lets_the_next_step_run():
    """Two landlords answered and one went silent — that is an answer, not a dead end."""
    plan_id = _plan()
    asks = [plans.add_step(plan_id, f"ask landlord {i}") for i in range(3)]
    compare = plans.add_step(plan_id, "compare the answers", depends_on=asks)

    plans.finish_step(asks[0], "answered: 800/mo")
    plans.finish_step(asks[1], "answered: 950/mo")
    for _ in range(plans.MAX_STEP_ATTEMPTS + 1):
        plans.mark_running(asks[2])
        given_up = plans.fail_step(asks[2], "no reply")

    assert given_up is True
    assert [s["id"] for s in plans.ready_steps(AGENT)] == [compare]


def test_the_next_step_can_read_what_the_previous_ones_produced():
    plan_id = _plan()
    first = plans.add_step(plan_id, "ask")
    second = plans.add_step(plan_id, "compare", depends_on=[first])
    plans.finish_step(first, "800 per month, deposit two months")

    results = plans.dependency_results(plans.get_step(second))

    assert [r["result"] for r in results] == ["800 per month, deposit two months"]


def test_a_step_parked_in_the_future_is_not_ready():
    plan_id = _plan()
    step_id = plans.add_step(plan_id, "check the price", wait={"kind": "at"})
    plans.park_step(step_id, {"kind": "at"}, wake_at=_soon())

    assert plans.ready_steps(AGENT) == []


def test_a_parked_step_becomes_ready_when_its_wake_time_passes():
    plan_id = _plan()
    step_id = plans.add_step(plan_id, "check the price", wait={"kind": "at"})
    plans.park_step(step_id, {"kind": "at"}, wake_at=1.0)

    assert [s["id"] for s in plans.ready_steps(AGENT)] == [step_id]


def test_steps_of_a_cancelled_plan_are_never_ready():
    plan_id = _plan()
    plans.add_step(plan_id, "search")
    plans.cancel_plan(plan_id, AGENT)

    assert plans.ready_steps(AGENT) == []


def test_another_agents_plan_is_none_of_our_business():
    other = plans.create_plan(agent_name="nexus", goal="theirs")
    plans.add_step(other, "their step")

    assert plans.ready_steps(AGENT) == []


# --- retries and give-up ------------------------------------------------------


def test_a_transient_failure_is_retried_rather_than_ending_the_plan():
    plan_id = _plan()
    step_id = plans.add_step(plan_id, "fetch the listing")

    plans.mark_running(step_id)
    given_up = plans.fail_step(step_id, "connection reset")

    step = plans.get_step(step_id)
    assert given_up is False
    assert step["state"] == plans.STEP_PENDING
    assert step["wake_at"] > 0, "a retry should not hammer the same failure immediately"
    assert plans.get_plan(plan_id)["state"] == plans.PLAN_ACTIVE


def test_a_step_that_keeps_failing_is_given_up_on():
    plan_id = _plan()
    step_id = plans.add_step(plan_id, "fetch the listing")

    for _ in range(plans.MAX_STEP_ATTEMPTS):
        plans.mark_running(step_id)
        given_up = plans.fail_step(step_id, "connection reset")

    assert given_up is True
    assert plans.get_step(step_id)["state"] == plans.STEP_FAILED
    assert "attempts" in plans.get_step(step_id)["result"]


def test_a_condition_that_never_fires_eventually_gives_up():
    """Otherwise the poller pays for it on every cycle until the heat death."""
    plan_id = _plan()
    step_id = plans.add_step(plan_id, "wait for a price drop", wait={"kind": "page_number"})
    plans._db().write("UPDATE plan_steps SET checks = ? WHERE id = ?", (plans.MAX_CONDITION_CHECKS - 1, step_id))

    assert plans.note_check(step_id, next_check_at=1.0) is True
    assert plans.get_step(step_id)["state"] == plans.STEP_FAILED
    assert "never fired" in plans.get_step(step_id)["result"]


def test_a_check_that_did_not_fire_just_schedules_the_next_one():
    plan_id = _plan()
    step_id = plans.add_step(plan_id, "wait", wait={"kind": "page_number"})

    assert plans.note_check(step_id, next_check_at=1234.0) is False

    step = plans.get_step(step_id)
    assert step["checks"] == 1
    assert step["wake_at"] == 1234.0
    assert step["state"] == plans.STEP_WAITING


def test_releasing_a_step_clears_the_condition():
    plan_id = _plan()
    step_id = plans.add_step(plan_id, "act on the drop", wait={"kind": "at"})

    plans.release_step(step_id)

    step = plans.get_step(step_id)
    assert step["state"] == plans.STEP_PENDING
    assert plans.wait_spec(step) == {}


# --- closing ------------------------------------------------------------------


def test_a_plan_stays_active_until_every_step_stops_moving():
    plan_id = _plan()
    first = plans.add_step(plan_id, "one")
    plans.add_step(plan_id, "two")
    plans.finish_step(first, "done")

    assert plans.settle_plan(plan_id) == plans.PLAN_ACTIVE


def test_a_plan_with_no_steps_is_not_finished():
    """An empty plan is one being written, not one that succeeded."""
    assert plans.settle_plan(_plan()) == plans.PLAN_ACTIVE


def test_a_plan_that_got_somewhere_is_done_even_with_a_failed_step():
    plan_id = _plan()
    good = plans.add_step(plan_id, "one")
    bad = plans.add_step(plan_id, "two")
    plans.finish_step(good, "found three flats")
    for _ in range(plans.MAX_STEP_ATTEMPTS):
        plans.mark_running(bad)
        plans.fail_step(bad, "site down")

    assert plans.settle_plan(plan_id) == plans.PLAN_DONE


def test_a_plan_where_nothing_worked_is_failed():
    plan_id = _plan()
    step_id = plans.add_step(plan_id, "one")
    for _ in range(plans.MAX_STEP_ATTEMPTS):
        plans.mark_running(step_id)
        plans.fail_step(step_id, "site down")

    assert plans.settle_plan(plan_id) == plans.PLAN_FAILED


def test_cancelling_is_idempotent_and_scoped_to_the_agent():
    plan_id = _plan()

    assert plans.cancel_plan(plan_id, "nexus") is False, "another agent's plan is not ours to cancel"
    assert plans.cancel_plan(plan_id, AGENT) is True
    assert plans.cancel_plan(plan_id, AGENT) is False


def test_an_expired_plan_says_so_instead_of_waiting_forever():
    plan_id = plans.create_plan(agent_name=AGENT, goal="watch a price", ttl_seconds=-1)
    step_id = plans.add_step(plan_id, "keep watching", wait={"kind": "page_number"})

    expired = plans.expired_plans(AGENT)
    assert [p["id"] for p in expired] == [plan_id]

    plans.expire_plan(plan_id)

    assert plans.get_plan(plan_id)["state"] == plans.PLAN_FAILED
    assert "expired" in plans.get_step(step_id)["result"]
    assert plans.ready_steps(AGENT) == []


def test_a_summary_is_kept_on_the_plan():
    plan_id = _plan()

    plans.set_summary(plan_id, "  three flats, two landlords answered  ")

    assert plans.get_plan(plan_id)["summary"] == "three flats, two landlords answered"


def _soon() -> float:
    import time

    return time.time() + 3600
