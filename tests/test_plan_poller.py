"""The poller: what runs, what waits, and what reaches the owner.

The expensive mistakes here are all about volume. Running every ready step in
one cycle spends a day's budget in a minute; letting one plan take every slot
starves the rest; delivering each step's result turns a week-long watch into a
reason to switch notifications off. Each has a test.
"""

import pytest

from kronos import plans
from kronos.config import settings
from kronos.cron import plans as poller

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


class FakeAgent:
    def __init__(self, reply="сделано"):
        self.reply = reply
        self.calls: list[dict] = []

    async def ainvoke(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture
def agent(monkeypatch):
    """The live agent, plus a record of everything delivered to the owner."""
    sent: list[tuple[str, int]] = []

    def install(reply="сделано") -> tuple[FakeAgent, list]:
        fake = FakeAgent(reply)
        monkeypatch.setattr("kronos.bridge.get_agent", lambda: fake)
        monkeypatch.setattr(
            poller,
            "send_webhook",
            lambda text, chat_id=None, parse_mode=None, topic_id=None: sent.append((text, chat_id)) or True,
        )
        return fake, sent

    return install


def _plan(goal="найти жильё", **kwargs) -> int:
    return plans.create_plan(agent_name=AGENT, goal=goal, chat_id=77, thread_id="77", **kwargs)


# --- running a step -----------------------------------------------------------


async def test_a_ready_step_runs_and_keeps_its_result(agent):
    fake, _ = agent("нашёл три варианта")
    plan_id = _plan()
    step_id = plans.add_step(plan_id, "поищи на airbnb")

    await poller.run_due_plan_steps()

    assert plans.get_step(step_id)["state"] == plans.STEP_DONE
    assert plans.get_step(step_id)["result"] == "нашёл три варианта"
    assert fake.calls[0]["thread_id"] == f"plan:{plan_id}", "plan turns belong in the plan's own thread"


async def test_the_prompt_carries_the_goal_and_earlier_results(agent):
    fake, _ = agent()
    plan_id = _plan("найти жильё на Бали")
    first = plans.add_step(plan_id, "спроси у арендодателя про депозит", title="аренда 1")
    plans.finish_step(first, "депозит два месяца")
    plans.add_step(plan_id, "сравни условия", depends_on=[first])

    await poller.run_due_plan_steps()

    prompt = fake.calls[0]["message"]  # calls[-1] is the closing summary
    assert "найти жильё на Бали" in prompt
    assert "депозит два месяца" in prompt
    assert "сравни условия" in prompt


async def test_a_failed_dependency_is_stated_plainly_in_the_prompt(agent):
    fake, _ = agent()
    plan_id = _plan()
    asked = plans.add_step(plan_id, "спроси", title="аренда 2")
    for _ in range(plans.MAX_STEP_ATTEMPTS):
        plans.mark_running(asked)
        plans.fail_step(asked, "не ответил")
    plans.add_step(plan_id, "сравни", depends_on=[asked])

    await poller.run_due_plan_steps()

    assert "НЕ УДАЛОСЬ" in fake.calls[0]["message"]


async def test_an_agent_that_is_not_up_yet_costs_the_step_nothing(monkeypatch):
    monkeypatch.setattr("kronos.bridge.get_agent", lambda: None)
    plan_id = _plan()
    step_id = plans.add_step(plan_id, "поищи")

    await poller.run_due_plan_steps()

    step = plans.get_step(step_id)
    assert step["attempts"] == 0, "a restart window must not burn the retry budget"
    assert step["state"] == plans.STEP_PENDING


async def test_a_step_whose_turn_raised_is_retried(agent):
    agent(RuntimeError("provider down"))
    plan_id = _plan()
    step_id = plans.add_step(plan_id, "поищи")

    await poller.run_due_plan_steps()

    step = plans.get_step(step_id)
    assert step["state"] == plans.STEP_PENDING
    assert "provider down" in step["result"]


async def test_an_empty_reply_is_not_a_result(agent):
    agent("   ")
    plan_id = _plan()
    step_id = plans.add_step(plan_id, "поищи")

    await poller.run_due_plan_steps()

    assert plans.get_step(step_id)["state"] == plans.STEP_PENDING
    assert "nothing" in plans.get_step(step_id)["result"]


# --- volume -------------------------------------------------------------------


async def test_only_a_few_steps_run_per_cycle(agent):
    """Forty ready steps in one minute would be a day's budget in a minute."""
    fake, _ = agent()
    for i in range(10):
        plans.add_step(_plan(f"план {i}"), "работай")

    await poller.run_due_plan_steps()

    steps_run = [c for c in fake.calls if "Ты выполняешь шаг" in c["message"]]
    assert len(steps_run) == poller.MAX_STEPS_PER_CYCLE


async def test_one_plan_does_not_take_every_slot(agent):
    fake, _ = agent()
    busy = _plan("занятой план")
    for i in range(5):
        plans.add_step(busy, f"работа {i}")
    other = _plan("другой план")
    plans.add_step(other, "тоже работа")

    await poller.run_due_plan_steps()

    threads = [call["thread_id"] for call in fake.calls]
    assert threads.count(f"plan:{busy}") == 1
    assert f"plan:{other}" in threads


# --- waiting ------------------------------------------------------------------


async def test_a_step_whose_condition_has_not_fired_is_not_run(agent, monkeypatch):
    fake, _ = agent()
    plan_id = _plan()
    step_id = plans.add_step(plan_id, "действуй по падению цены", wait={"kind": "page_number"})
    _condition(monkeypatch, fired=False, next_check_at=999.0)

    await poller.run_due_plan_steps()

    step = plans.get_step(step_id)
    assert fake.calls == []
    assert step["state"] == plans.STEP_WAITING
    assert step["checks"] == 1
    assert step["wake_at"] == 999.0


async def test_a_condition_that_fired_runs_the_step_with_what_was_seen(agent, monkeypatch):
    fake, _ = agent()
    plan_id = _plan()
    step_id = plans.add_step(plan_id, "купи", wait={"kind": "page_number"})
    _condition(monkeypatch, fired=True, detail="цена теперь 8 750 000, ниже 9 000 000")

    await poller.run_due_plan_steps()

    assert "8 750 000" in fake.calls[0]["message"]
    assert plans.get_step(step_id)["state"] == plans.STEP_DONE


async def test_condition_checks_are_capped_per_cycle(agent, monkeypatch):
    """Page conditions fetch, and twenty fetches is already most of a minute."""
    agent()
    for i in range(poller.MAX_CHECKS_PER_CYCLE + 5):
        plans.add_step(_plan(f"наблюдение {i}"), "смотри", wait={"kind": "page_number"})
    checked = _condition(monkeypatch, fired=False, next_check_at=999.0)

    await poller.run_due_plan_steps()

    assert len(checked) == poller.MAX_CHECKS_PER_CYCLE


# --- what reaches the owner ---------------------------------------------------


async def test_a_step_stays_quiet_unless_it_was_asked_to_speak(agent):
    _, sent = agent("промежуточный результат")
    plan_id = _plan()
    plans.add_step(plan_id, "шаг 1")
    plans.add_step(plan_id, "шаг 2")

    await poller.run_due_plan_steps()

    assert sent == [], "an hourly watch that messages every cycle gets muted"


async def test_a_step_marked_notify_reaches_the_owner(agent):
    _, sent = agent("цена упала")
    plan_id = _plan()
    plans.add_step(plan_id, "скажи мне", notify=True)
    plans.add_step(plan_id, "и ещё поработай")

    await poller.run_due_plan_steps()

    assert sent[0] == ("цена упала", 77)


async def test_a_finished_plan_is_summarised_once(agent):
    fake, sent = agent("итог: два варианта подходят")
    plan_id = _plan()
    plans.add_step(plan_id, "единственный шаг")

    await poller.run_due_plan_steps()

    assert plans.get_plan(plan_id)["state"] == plans.PLAN_DONE
    assert plans.get_plan(plan_id)["summary"] == "итог: два варианта подходят"
    assert len(sent) == 1
    assert fake.calls[-1]["persist_user_turn"] is False, "the summary is for the owner, not the history"


async def test_a_plan_still_working_is_not_summarised(agent):
    _, sent = agent()
    plan_id = _plan()
    plans.add_step(plan_id, "шаг 1")
    plans.add_step(plan_id, "шаг 2", wait={"kind": "manual"})

    await poller.run_due_plan_steps()

    assert plans.get_plan(plan_id)["state"] == plans.PLAN_ACTIVE
    assert sent == []


async def test_an_expired_plan_is_retired_and_reported(agent):
    """Silence would leave the owner believing it is still watching."""
    _, sent = agent("план истёк, ничего не нашлось")
    plan_id = _plan(ttl_seconds=-1)
    plans.add_step(plan_id, "наблюдай", wait={"kind": "manual"})

    await poller.run_due_plan_steps()

    assert plans.get_plan(plan_id)["state"] == plans.PLAN_FAILED
    assert len(sent) == 1


async def test_a_summary_survives_the_agent_being_down(monkeypatch):
    """The step results are a worse message than a written summary, but not no message."""
    sent: list = []
    monkeypatch.setattr("kronos.bridge.get_agent", lambda: None)
    monkeypatch.setattr(
        poller, "send_webhook", lambda text, chat_id=None, parse_mode=None, topic_id=None: sent.append(text) or True
    )
    plan_id = _plan("найти жильё")
    step_id = plans.add_step(plan_id, "шаг")
    plans.finish_step(step_id, "нашёл вариант")

    await poller._summarize(plans.get_plan(plan_id), "выполнен")

    assert "найти жильё" in sent[0]
    assert "нашёл вариант" in sent[0]


async def test_a_plan_with_no_chat_behind_it_delivers_nothing(agent):
    _, sent = agent()
    plan_id = plans.create_plan(agent_name=AGENT, goal="фоновая работа")
    plans.add_step(plan_id, "шаг", notify=True)

    await poller.run_due_plan_steps()

    assert sent == []
    assert plans.get_plan(plan_id)["summary"], "the summary is still recorded, just not sent"


def _condition(monkeypatch, *, fired: bool, detail: str = "", next_check_at: float = 0.0) -> list:
    """Replace condition evaluation; returns the list of steps it was asked about."""
    checked: list = []

    async def fake_evaluate(spec, *, step, plan, now=None):
        checked.append(step["id"])
        from kronos.plan_conditions import Verdict

        return Verdict(fired, detail, next_check_at)

    monkeypatch.setattr("kronos.plan_conditions.evaluate", fake_evaluate)
    return checked
