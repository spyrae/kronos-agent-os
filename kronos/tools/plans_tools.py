"""Letting the agent declare work that will not finish today.

The turn the plan is written in is short; the plan is not. So these tools only
*declare* — a goal, steps, and what each step waits for. The poller
(`kronos.cron.plans`) is what runs them, one at a time, for as long as it takes.

Waiting conditions are passed as JSON because they are structured and vary by
kind, and a malformed one is refused with the reason attached — an agent that
gets "op must be one of below, above" can fix it in the same turn, which is the
whole reason validation happens here rather than next Tuesday.
"""

import json
import logging

from langchain_core.tools import tool

from kronos import plan_conditions, plans
from kronos.audit import get_tool_audit_context
from kronos.config import settings
from kronos.security.effects import mark_side_effect

log = logging.getLogger("kronos.tools.plans")

MAX_LISTED_STEPS = 20


def _chat_context() -> tuple[int, str, int | None]:
    """(chat_id, thread_id, topic_id) of the request, so results can come back."""
    ctx = get_tool_audit_context()
    chat_raw = ctx.get("session_id", "")
    thread_id = ctx.get("thread_id", "")
    topic_id = None
    _, sep, tail = thread_id.partition(":")
    if sep and tail.isdigit():
        topic_id = int(tail)
    try:
        chat_id = int(chat_raw)
    except (TypeError, ValueError):
        chat_id = 0
    return chat_id, thread_id, topic_id


def _parse_wait(raw: str) -> tuple[dict | None, str]:
    """(condition, error). Empty input means the step simply waits its turn."""
    raw = (raw or "").strip()
    if not raw:
        return None, ""
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"wait должен быть JSON-объектом: {e}"
    try:
        normalized, _ = plan_conditions.normalize(spec)
    except plan_conditions.ConditionError as e:
        return None, f"Условие ожидания не годится: {e}"
    except Exception as e:  # egress policy, mostly
        return None, f"Условие ожидания отклонено: {e}"
    return normalized, ""


def _parse_after(raw: str) -> tuple[list[int], str]:
    ids = []
    for part in (raw or "").replace(" ", "").split(","):
        if not part:
            continue
        if not part.lstrip("-").isdigit():
            return [], f"after_steps должен быть списком номеров шагов, а не '{part}'"
        ids.append(int(part))
    return ids, ""


def _render_plan(plan: dict, *, with_steps: bool = True) -> str:
    lines = [f"План #{plan['id']} ({plan['state']}): {plan['goal']}"]
    if plan.get("summary"):
        lines.append(f"Итог: {plan['summary']}")
    if not with_steps:
        return "\n".join(lines)

    for step in plans.steps_of(plan["id"])[:MAX_LISTED_STEPS]:
        label = step["title"] or f"шаг {step['seq']}"
        line = f"  #{step['id']} {label} — {step['state']}"
        spec = plans.wait_spec(step)
        if spec:
            line += f", ждёт {plan_conditions.describe(spec)}"
        deps = plans.dependency_ids(step)
        if deps:
            line += f", после {', '.join(f'#{d}' for d in deps)}"
        if step["result"]:
            line += f"\n     → {step['result'][:300]}"
        lines.append(line)
    return "\n".join(lines)


@tool
def plan_start(goal: str, first_step: str, title: str = "") -> str:
    """Start a plan: work that will take days and mostly consist of waiting.

    Use this when the answer cannot be reached in this conversation — waiting for
    replies, watching a price, checking back after an event. Do NOT use it for
    work you can simply do now, or for a plain reminder (use schedule_task).

    Add the remaining steps with plan_add_step. Each step runs later, on its own,
    and its result is kept for the steps that depend on it.

    Args:
        goal: what the plan is for, in one sentence — the owner reads this.
        first_step: what to do first, as an instruction to yourself.
        title: optional short label for that first step.
    """
    chat_id, thread_id, topic_id = _chat_context()
    try:
        plan_id = plans.create_plan(
            agent_name=settings.agent_name,
            goal=goal,
            chat_id=chat_id,
            topic_id=topic_id,
            thread_id=thread_id,
        )
        step_id = plans.add_step(plan_id, first_step, title=title)
    except plans.PlanError as e:
        return f"[ERROR] {e}"

    return (
        f"План #{plan_id} создан, шаг #{step_id} поставлен в очередь. "
        f"Добавляй следующие шаги через plan_add_step(plan_id={plan_id}). "
        f"Скажи пользователю, что вернёшься с результатом сам."
    )


@tool
def plan_add_step(
    plan_id: int,
    task: str,
    title: str = "",
    after_steps: str = "",
    wait: str = "",
    notify: bool = False,
) -> str:
    """Add a step to a plan, optionally parked until something happens.

    Args:
        plan_id: which plan.
        task: what to do, as an instruction to yourself when it wakes.
        title: optional short label.
        after_steps: step numbers this one needs first, e.g. "12,13". A step runs
            once they have all finished — including any that failed, whose result
            says so.
        wait: JSON condition to wait for. One of:
            {"kind":"at","seconds":86400} — or {"kind":"at","timestamp":<unix>}
            {"kind":"manual","note":"after I view the flat"} — until the owner resumes it
            {"kind":"reply"} — until the owner writes in the chat this plan came from
            {"kind":"page_matches","url":"https://...","pattern":"in stock","absent":false}
            {"kind":"page_number","url":"https://...","pattern":"Rp ([0-9.]+)","op":"below","value":9000000}
            Page conditions accept "every_seconds" (minimum 300, default 3600).
        notify: send this step's result to the owner as soon as it is done. Leave
            false unless it is worth interrupting them for — the plan's closing
            summary always reaches them anyway.
    """
    condition, error = _parse_wait(wait)
    if error:
        return f"[ERROR] {error}"
    deps, error = _parse_after(after_steps)
    if error:
        return f"[ERROR] {error}"

    try:
        step_id = plans.add_step(
            plan_id,
            task,
            title=title,
            depends_on=deps,
            wait=condition,
            notify=notify,
        )
    except plans.PlanError as e:
        return f"[ERROR] {e}"

    waiting = f", ждёт {plan_conditions.describe(condition)}" if condition else ""
    return f"Шаг #{step_id} добавлен в план #{plan_id}{waiting}."


@tool
def plan_status(plan_id: int = 0) -> str:
    """Show your plans: what is running, what is waiting and for what.

    Args:
        plan_id: one plan, or 0 for every plan still running.
    """
    if plan_id:
        plan = plans.get_plan(plan_id)
        if not plan:
            return f"[ERROR] Плана #{plan_id} нет."
        return _render_plan(plan)

    active = plans.list_plans(settings.agent_name)
    if not active:
        return "Активных планов нет."
    return "\n\n".join(_render_plan(plan) for plan in active)


@tool
def plan_cancel(plan_id: int, reason: str = "") -> str:
    """Stop a plan. Its waiting steps are dropped and nothing else runs.

    Args:
        plan_id: which plan.
        reason: why, for the record.
    """
    if not plans.cancel_plan(plan_id, settings.agent_name):
        return f"[ERROR] План #{plan_id} не найден или уже закрыт."
    log.info("Plan #%s cancelled: %s", plan_id, reason or "no reason given")
    return f"План #{plan_id} остановлен."


# Declaring a plan schedules future model calls and future page fetches, which is
# an effect outside this turn — durable resume must not replay it and create the
# plan twice.
PLAN_TOOLS = mark_side_effect([plan_start, plan_add_step, plan_cancel], reason="plans") + [plan_status]
