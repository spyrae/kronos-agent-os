"""The poller that makes a plan move.

Once a minute: retire plans that ran out of time, ask each parked step's
condition whether it is time, and run the steps that are ready. A step runs as
an ordinary agent turn, so durable turns, the effects ledger, approvals and the
cost guardian all apply without knowing plans exist.

Three things keep the loop honest:

* **Steps run in the plan's own thread** (``plan:<id>``), not the owner's chat.
  A dozen machine turns do not belong in a person's conversation, and it means a
  "wait for my reply" condition cannot mistake the agent's own work for the
  owner speaking.
* **One step per plan per cycle, a few steps in total.** Each step is a model
  call. Without the cap, one plan with forty ready steps would spend a day's
  budget in a minute and starve every other plan.
* **Nothing is delivered unless it was asked for.** A step notifies only when it
  was created with ``notify``; what always arrives is the plan finishing. A
  week-long watch that sent a message every hour would be turned off in a day.
"""

import asyncio
import logging

from kronos import plan_conditions, plans
from kronos.config import settings
from kronos.cron.notify import send_webhook

log = logging.getLogger("kronos.cron.plans")

# Step runs per cycle, across all plans. A minute of wall clock is not a reason
# to run everything that happens to be ready.
MAX_STEPS_PER_CYCLE = 3
# Closing summaries per cycle. Counted separately from steps because a plan that
# finished is what the owner is actually waiting for, and it must not be crowded
# out by steps of other plans. Leftovers are picked up next cycle — a plan is
# owed a summary until it has one.
MAX_SUMMARIES_PER_CYCLE = 2
# Condition checks per cycle. Page conditions fetch, and a fetch can take
# seconds — twenty is already most of a minute.
MAX_CHECKS_PER_CYCLE = 20
MAX_RESULT_CHARS_IN_PROMPT = 1200


def plan_thread_id(plan_id: int) -> str:
    return f"plan:{plan_id}"


def _short(text: str, limit: int = MAX_RESULT_CHARS_IN_PROMPT) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def step_prompt(plan: dict, step: dict, observation: str = "") -> str:
    """What the agent is asked when a step wakes.

    Carries the goal, what earlier steps produced (including what failed, plainly
    said), and the observation that woke this one — a condition already fetched
    the page, and making the step fetch it again to find out why it woke would be
    both slower and a chance to see something different.
    """
    lines = [
        "Ты выполняешь шаг долгоживущего плана.",
        "",
        f"Цель плана: {plan['goal']}",
        f"Шаг {step['seq']}" + (f": {step['title']}" if step["title"] else ""),
    ]
    if observation:
        lines += ["", f"Что произошло: {observation}"]

    done = plans.dependency_results(step)
    if done:
        lines += ["", "Результаты шагов, от которых зависит этот:"]
        for dep in done:
            label = dep["title"] or f"шаг {dep['seq']}"
            if dep["state"] == plans.STEP_FAILED:
                lines.append(f"- {label}: НЕ УДАЛОСЬ — {_short(dep['result'], 300)}")
            else:
                lines.append(f"- {label}: {_short(dep['result'])}")

    lines += [
        "",
        f"Задача: {step['prompt']}",
        "",
        "Ответь результатом шага — кратко и по делу, это будет сохранено в план. "
        "Если работа продолжается позже, добавь следующий шаг через plan_add_step "
        f"(plan_id={plan['id']}) с условием ожидания.",
    ]
    return "\n".join(lines)


async def _run_step(plan: dict, step: dict, observation: str) -> None:
    """Run one step as a turn. Its reply is the step's result."""
    from kronos.bridge import get_agent

    agent = get_agent()
    if agent is None:
        log.warning("Plan step #%s skipped: agent not ready", step["id"])
        return

    plans.mark_running(step["id"])
    try:
        result = await agent.ainvoke(
            message=step_prompt(plan, step, observation),
            thread_id=plan_thread_id(plan["id"]),
            user_id="plan",
            session_id=str(plan["chat_id"] or plan["id"]),
            source_kind="user",
            persist_user_turn=True,
        )
    except Exception as e:
        log.error("Plan step #%s failed: %s", step["id"], e)
        plans.fail_step(step["id"], str(e))
        return

    text = (result or "").strip()
    if not text:
        # An empty reply is not a result. Retrying is the right reading: the
        # model was cut off, the provider hiccuped, the turn was blocked.
        plans.fail_step(step["id"], "the agent returned nothing")
        return

    plans.finish_step(step["id"], text)
    if step["notify"]:
        await _deliver(plan, text)


async def _deliver(plan: dict, text: str) -> None:
    if not plan.get("chat_id"):
        return
    await asyncio.to_thread(send_webhook, text, plan["chat_id"], None, plan.get("topic_id"))


async def _summarize(plan: dict, state: str) -> None:
    """Say how it went, once, when the plan closes.

    This is the message the owner actually waited days for, so it is worth a
    model call: the raw step results are a log, not an answer.
    """
    steps = plans.steps_of(plan["id"])
    rendered = []
    for step in steps:
        label = step["title"] or f"шаг {step['seq']}"
        mark = "не удалось" if step["state"] == plans.STEP_FAILED else "готово"
        rendered.append(f"- {label} ({mark}): {_short(step['result'], 600)}")

    from kronos.bridge import get_agent

    agent = get_agent()
    fallback = f"План «{plan['goal']}» — {state}.\n\n" + "\n".join(rendered)
    if agent is None:
        plans.set_summary(plan["id"], fallback)
        await _deliver(plan, fallback)
        return

    prompt = (
        f"Долгоживущий план завершён ({state}).\n\n"
        f"Цель: {plan['goal']}\n\n"
        f"Что вышло по шагам:\n" + "\n".join(rendered) + "\n\n"
        "Напиши пользователю итог: что удалось, что нет, что делать дальше. "
        "Без пересказа процесса — только результат и следующий шаг."
    )
    try:
        summary = await agent.ainvoke(
            message=prompt,
            thread_id=plan_thread_id(plan["id"]),
            user_id="plan",
            session_id=str(plan["chat_id"] or plan["id"]),
            source_kind="user",
            persist_user_turn=False,
        )
    except Exception as e:
        log.error("Plan #%s summary failed: %s", plan["id"], e)
        summary = ""

    text = (summary or "").strip() or fallback
    plans.set_summary(plan["id"], text)
    await _deliver(plan, text)


async def _check_condition(plan: dict, step: dict) -> tuple[bool, str]:
    """Ask a parked step's condition. Returns (may run, what was observed)."""
    spec = plans.wait_spec(step)
    if not spec:
        return True, ""

    verdict = await plan_conditions.evaluate(spec, step=step, plan=plan)
    if verdict.notes:
        log.info("Step #%s condition notes: %s", step["id"], "; ".join(verdict.notes))

    if verdict.fired:
        plans.release_step(step["id"])
        return True, verdict.detail

    plans.note_check(step["id"], verdict.next_check_at)
    return False, ""


async def _retire_expired() -> None:
    """Mark plans that ran out of time. The summary pass reports them."""
    for plan in plans.expired_plans(settings.agent_name):
        plans.expire_plan(plan["id"])


async def _deliver_pending_summaries(limit: int) -> int:
    """Close out plans that finished but have not said so yet. Returns how many.

    A plan owes the owner a summary until it has one, which is also what makes
    this crash-safe: the marker is the empty summary on a finished plan, not a
    variable in a loop that just died.
    """
    if limit <= 0:
        return 0
    pending = plans.plans_awaiting_summary(settings.agent_name, limit=limit)
    for plan in pending:
        await _summarize(plan, "выполнен" if plan["state"] == plans.PLAN_DONE else "не удался")
    return len(pending)


async def run_due_plan_steps() -> None:
    """One cycle: retire what timed out, report what closed, run what is ready."""
    await _retire_expired()
    # Last cycle's leftovers first: a finished plan is what the owner is waiting
    # for, and it should not queue behind other plans' steps.
    summaries = MAX_SUMMARIES_PER_CYCLE - await _deliver_pending_summaries(MAX_SUMMARIES_PER_CYCLE)

    ready = plans.ready_steps(settings.agent_name)
    if not ready:
        return

    ran = 0
    checks = 0
    seen_plans: set[int] = set()
    touched_plans: set[int] = set()

    for step in ready:
        plan_id = step["plan_id"]
        if plan_id in seen_plans:
            continue  # one step per plan per cycle keeps plans from starving each other
        if ran >= MAX_STEPS_PER_CYCLE:
            break

        plan = plans.get_plan(plan_id)
        if not plan or plan["state"] != plans.PLAN_ACTIVE:
            continue

        if plans.wait_spec(step):
            if checks >= MAX_CHECKS_PER_CYCLE:
                break
            checks += 1
            may_run, observation = await _check_condition(plan, step)
            if not may_run:
                continue
        else:
            observation = ""

        seen_plans.add(plan_id)
        touched_plans.add(plan_id)
        await _run_step(plan, plans.get_step(step["id"]), observation)
        ran += 1

    for plan_id in touched_plans:
        plans.settle_plan(plan_id)
    # Anything that just closed gets its summary now rather than a minute later;
    # what does not fit in the cycle's budget is still owed one.
    await _deliver_pending_summaries(summaries)
