"""The poller that makes a plan move.

Once a minute: retire plans that ran out of time, ask each parked step's
condition whether it is time, and run the steps that are ready. A step runs as
a durable agent turn with effects and approval evidence. Budget enforcement
across all callers is a separate contract; a per-cycle cap is not a cost limit.

Three things keep the loop honest:

* **Steps run in the plan's own thread** (``plan:<id>``), not the owner's chat.
  A dozen machine turns do not belong in a person's conversation, and it means a
  "wait for my reply" condition cannot mistake the agent's own work for the
  owner speaking.
* **One step per plan per cycle, a few steps in total.** Each step is a model
  call. Without the cap, one plan with forty ready steps would spend a day's
  budget in a minute and starve every other plan.
* **Routine progress is queued only when asked for.** Approval requests
  notify the owner because work cannot continue without a decision. A step queues
  a result only when created with ``notify``; the final summary has its own
  durable delivery obligation. A
  week-long watch that sent a message every hour would be turned off in a day.
"""

import logging
import time

from kronos import plan_conditions, plans
from kronos.config import settings
from kronos.outcomes import InvocationOutcome
from kronos.policy import get_policy
from kronos.session import SessionStore
from kronos.turn_ownership import TurnBusyError, TurnOwnership, own_conversation

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


async def _recover_unlinked(step: dict, store: SessionStore, ownership: TurnOwnership) -> dict | None:
    """Repair the two-database crash window; absence alone is not legacy proof."""
    if step.get("turn_id"):
        return step
    key = step.get("execution_key", "")
    if not key:
        plans.update_linked_step(
            step["id"],
            "",
            state=plans.STEP_REVIEW,
            result="Legacy unlinked execution; review effects before retrying.",
        )
        return None
    turn = await store.get_turn_for_caller(key)
    if turn is None:
        plans.retry_unstarted_step(step, ownership=ownership)
        return None
    if turn["thread_id"] != plan_thread_id(step["plan_id"]):
        plans.update_linked_step(step["id"], "", state=plans.STEP_REVIEW, result="Caller correlation mismatch.")
        return None
    plans.link_turn(step["id"], turn["turn_id"], execution_key=key)
    return plans.get_step(step["id"])


async def _run_step(plan: dict, step: dict, observation: str) -> None:
    """Own the conversation before claiming work, through outcome persistence."""
    from kronos.bridge import get_agent

    agent = get_agent()
    if agent is None or agent.session_store is None:
        log.warning("Plan step #%s skipped: durable agent not ready", step["id"])
        return
    store = agent.session_store
    try:
        async with own_conversation(store.db_path, plan_thread_id(plan["id"]), wait=False) as ownership:
            if not plans.claim_step(step["id"], ownership=ownership):
                return
            current = plans.get_step(step["id"])
            try:
                if await store.get_turn_for_caller(current["execution_key"]):
                    linked = await _recover_unlinked(current, store, ownership)
                    if linked:
                        await _apply_outcome(plan, linked, await agent.get_turn_outcome(linked["turn_id"]))
                    return
                result = await agent.ainvoke_outcome(
                    message=step_prompt(plan, current, observation),
                    thread_id=plan_thread_id(plan["id"]),
                    user_id="plan",
                    session_id=str(plan["chat_id"] or plan["id"]),
                    source_kind="user",
                    persist_user_turn=True,
                    caller_key=current["execution_key"],
                    execution_ownership=ownership,
                    on_turn_started=lambda turn_id: plans.link_turn(
                        step["id"],
                        turn_id,
                        execution_key=current["execution_key"],
                    ),
                )
            except Exception:
                # CancelledError deliberately propagates: the next poll owns
                # recovery only after this task has unwound and released flock.
                log.exception("Plan step #%s interrupted", step["id"])
                current = await _recover_unlinked(plans.get_step(step["id"]), store, ownership)
                if current:
                    plans.update_linked_step(
                        step["id"],
                        current["turn_id"],
                        state=plans.STEP_INTERRUPTED,
                        result="Execution interrupted; continuation must use the same turn.",
                    )
                return
            await _apply_outcome(plan, plans.get_step(step["id"]), result)
    except TurnBusyError:
        return


async def _apply_outcome(plan: dict, step: dict, outcome: InvocationOutcome) -> None:
    """Advance only from execution evidence, never from nonempty reply text."""
    turn_id = step.get("turn_id", "")
    if outcome.thread_id != plan_thread_id(plan["id"]) or (outcome.turn_id or "") != turn_id:
        plans.update_linked_step(
            step["id"],
            turn_id,
            state=plans.STEP_REVIEW,
            result="Turn correlation mismatch; review required.",
        )
        return
    text = outcome.content.strip()
    if outcome.status == "waiting_approval" and outcome.approval_id:
        if not plans.update_linked_step(
            step["id"],
            turn_id,
            state=plans.STEP_APPROVAL,
            result=text,
            approval_id=outcome.approval_id,
        ):
            return
        if step.get("notified_approval_id") != outcome.approval_id:
            from kronos.bridge import deliver_plan_approval

            if await deliver_plan_approval(turn_id, outcome.approval_id):
                plans.note_approval_delivered(step["id"], turn_id, outcome.approval_id)
        return
    if outcome.status == "running":
        if step["state"] != plans.STEP_WAITING:
            plans.update_linked_step(step["id"], turn_id, state=plans.STEP_RUNNING, result=step["result"])
        return
    if outcome.status == "completed" and text:
        plans.complete_step_turn(step["id"], turn_id, text)
        return
    if outcome.status in {"blocked", "rejected", "expired"}:
        state = plans.STEP_FAILED
        text = f"{outcome.status}: {text or 'operation not authorized'}"
    else:
        # A model failure/empty reply can follow real effects. A new turn would
        # bypass the old turn's deduplication and must not be an automatic retry.
        state = plans.STEP_REVIEW
        text = f"Review required ({outcome.reason or outcome.status}): {text or 'the agent returned nothing'}"
    plans.update_linked_step(step["id"], turn_id, state=state, result=text)


async def _reconcile_turns() -> set[int]:
    """Reconcile stopped executors under the same lock and the configured policy.

    Returns plans whose continuation consumed this cycle's execution budget.
    """
    from kronos.bridge import get_agent

    attempted: set[int] = set()
    agent = get_agent()
    if agent is None or agent.session_store is None:
        return attempted
    store = agent.session_store
    policy = get_policy().durable
    for candidate in plans.steps_to_reconcile(settings.agent_name):
        try:
            plan_id = candidate["plan_id"]
            async with own_conversation(store.db_path, plan_thread_id(plan_id), wait=False) as ownership:
                step = plans.get_step(candidate["id"])
                if not step or not (
                    step["state"] in {plans.STEP_RUNNING, plans.STEP_APPROVAL, plans.STEP_INTERRUPTED}
                    or (step["state"] == plans.STEP_WAITING and step["turn_id"])
                ):
                    continue
                plan = plans.get_plan(plan_id)
                if not plan or plan["state"] != plans.PLAN_ACTIVE or plan["expires_at"] <= time.time():
                    continue
                step = await _recover_unlinked(step, store, ownership)
                if not step:
                    plans.settle_plan(plan_id)
                    continue
                outcome = await agent.get_turn_outcome(step["turn_id"])
                if outcome.status == "running":
                    if policy.resume_mode != "resume":
                        plans.update_linked_step(
                            step["id"],
                            step["turn_id"],
                            state=plans.STEP_INTERRUPTED,
                            result="Execution stopped; report policy requires explicit turn resume.",
                        )
                        continue
                    if plan_id in attempted or len(attempted) >= MAX_STEPS_PER_CYCLE:
                        continue
                    attempted.add(plan_id)
                    await agent.resume_interrupted_turn(
                        step["turn_id"],
                        max_attempts=policy.max_resume_attempts,
                        execution_ownership=ownership,
                    )
                    outcome = await agent.get_turn_outcome(step["turn_id"])
                await _apply_outcome(plan, plans.get_step(step["id"]), outcome)
                plans.settle_plan(plan_id)
        except TurnBusyError:
            pass  # A live executor is never abandoned, regardless of its age.
        except Exception:
            log.exception("Plan step #%s reconciliation failed", candidate["id"])
        finally:
            plans.note_reconciled(candidate)
    return attempted


async def _reconcile_stops() -> None:
    """Finish stop cleanup without racing a live task, approval or process."""
    from kronos.bridge import get_agent

    agent = get_agent()
    store = (
        agent.session_store if agent is not None and agent.session_store is not None else SessionStore(settings.db_path)
    )
    for candidate in plans.stopped_steps(settings.agent_name):
        try:
            thread_id = plan_thread_id(candidate["plan_id"])
            async with own_conversation(store.db_path, thread_id, wait=False) as ownership:
                step = plans.get_step(candidate["id"])
                if not step:
                    continue
                plan = plans.get_plan(step["plan_id"])
                if (
                    not plan
                    or step["stop_reconciled"]
                    or plan["state"] not in {plans.PLAN_CANCELLED, plans.PLAN_FAILED}
                ):
                    continue
                reason = plans.stop_reason(plan)
                if not reason:
                    continue
                turn_id = step["turn_id"]
                if not turn_id and step["execution_key"]:
                    turn = await store.get_turn_for_caller(step["execution_key"])
                    if turn:
                        if turn["thread_id"] != thread_id:
                            plans.reconcile_stopped_step(
                                step,
                                turn_id="",
                                state=plans.STEP_REVIEW,
                                result="Stop correlation mismatch.",
                                ownership=ownership,
                            )
                            continue
                        turn_id = turn["turn_id"]
                state = plans.STEP_FAILED
                result = f"{reason}; execution stopped. Previously dispatched operations are not undone."
                if turn_id:
                    evidence = await store.stop_plan_turn(
                        turn_id, thread_id=thread_id, reason=reason, ownership=ownership
                    )
                    if evidence["review"]:
                        state = plans.STEP_REVIEW
                        result = f"{reason}; effects require review. {evidence.get('pending_effects', 0)} unresolved intent(s)."
                    elif evidence.get("completed"):
                        state, result = plans.STEP_DONE, evidence["content"]
                elif step["state"] in {plans.STEP_DONE, plans.STEP_FAILED}:
                    state, result = step["state"], step["result"]
                elif step["state"] == plans.STEP_REVIEW or (
                    step["state"] not in {plans.STEP_PENDING, plans.STEP_WAITING} and not step["execution_key"]
                ):
                    state, result = plans.STEP_REVIEW, f"{reason}; legacy execution identity missing; review effects."
                if step["state"] == plans.STEP_REVIEW:
                    state, result = plans.STEP_REVIEW, step["result"]
                plans.reconcile_stopped_step(step, turn_id=turn_id, state=state, result=result, ownership=ownership)
        except TurnBusyError:
            pass
        except Exception:
            log.exception("Stopped plan step #%s reconciliation failed", candidate["id"])
        finally:
            plans.note_reconciled(candidate)


async def _summarize(plan: dict, state: str) -> None:
    """Say how it went, once, when the plan closes.

    This is the message the owner actually waited days for, so it is worth a
    model call: the raw step results are a log, not an answer.
    """
    steps = plans.steps_of(plan["id"])
    rendered = []
    for step in steps:
        label = step["title"] or f"шаг {step['seq']}"
        mark = "готово" if step["state"] == plans.STEP_DONE else "не завершено / нужна проверка"
        rendered.append(f"- {label} ({mark}): {_short(step['result'], 600)}")

    from kronos.bridge import get_agent

    agent = get_agent()
    fallback = f"План «{plan['goal']}» — {state}.\n\n" + "\n".join(rendered)
    if agent is None or plans.stop_reason(plan):
        plans.set_summary(plan["id"], fallback)
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
            thread_id=f"plan-summary:{plan['id']}",
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


async def _check_condition(plan: dict, step: dict) -> tuple[bool, str]:
    """Ask a parked step's condition. Returns (may run, what was observed)."""
    spec = plans.wait_spec(step)
    if not spec:
        return True, ""

    verdict = await plan_conditions.evaluate(spec, step=step, plan=plan)
    if verdict.notes:
        log.info("Step #%s condition notes: %s", step["id"], "; ".join(verdict.notes))

    if verdict.fired:
        return plans.release_step(step["id"]), verdict.detail

    plans.note_check(step["id"], verdict.next_check_at)
    return False, ""


async def _retire_expired() -> None:
    """Mark plans that ran out of time. The summary pass reports them."""
    for plan in plans.expired_plans(settings.agent_name):
        plans.expire_plan(plan["id"])


async def _deliver_pending_summaries(limit: int) -> int:
    """Close out plans that finished but have not said so yet. Returns how many.

    Empty summary tracks generation. Its durable outbox tracks actual delivery;
    failed sends never require another model call or rerunning the plan.
    """
    if limit <= 0:
        return 0
    pending = plans.plans_awaiting_summary(settings.agent_name, limit=limit)
    for plan in pending:
        state = {plans.PLAN_DONE: "выполнен", plans.PLAN_CANCELLED: "остановлен"}.get(plan["state"], "не удался")
        await _summarize(plan, state)
    return len(pending)


async def run_due_plan_steps() -> None:
    """One cycle: retire what timed out, report what closed, run what is ready."""
    await _retire_expired()
    await _reconcile_stops()
    resumed = await _reconcile_turns()
    await _retire_expired()
    await _reconcile_stops()
    # Last cycle's leftovers first: a finished plan is what the owner is waiting
    # for, and it should not queue behind other plans' steps.
    summaries = MAX_SUMMARIES_PER_CYCLE - await _deliver_pending_summaries(MAX_SUMMARIES_PER_CYCLE)

    ready = plans.ready_steps(settings.agent_name)
    if not ready:
        return

    ran = len(resumed)
    checks = 0
    seen_plans: set[int] = set(resumed)
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

        spec = plans.wait_spec(step)
        if step.get("wait_json") and not spec:
            plans.update_linked_step(
                step["id"],
                "",
                state=plans.STEP_REVIEW,
                result="Unreadable condition; review required.",
            )
            continue
        if spec:
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
    await _retire_expired()
    await _reconcile_stops()
    # Anything that just closed gets its summary now rather than a minute later;
    # what does not fit in the cycle's budget is still owed one.
    await _deliver_pending_summaries(summaries)
