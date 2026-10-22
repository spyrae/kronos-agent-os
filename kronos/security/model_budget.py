"""Model-call admission, including models cached before a request starts."""

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from kronos.audit import get_tool_audit_context
from kronos.execution_control import check_execution


class ModelBudgetError(RuntimeError):
    """No new model request is allowed; this is not a provider failure."""


_force_lite: ContextVar[bool] = ContextVar("model_budget_force_lite", default=False)
# The hold of the model call being made in this context (see ``admit_model_call``).
_hold: ContextVar[str] = ContextVar("model_budget_hold", default="")
# True while a budgeted call is in flight. Code that runs inside it — a provider
# fallback chain, a lite model it delegates to — is covered by that call's hold
# and must not reserve a second one.
_admitted: ContextVar[bool] = ContextVar("model_budget_admitted", default=False)


def current_hold() -> str:
    """Hold reserved for the call in flight in this context, if any."""
    return _hold.get()


def budget_session_key(context: dict[str, str]) -> str:
    """The conversation a model call is charged to."""
    return context.get("session_id") or context.get("thread_id", "")


def finish_hold(hold_id: str, error: BaseException | None = None) -> None:
    """Close the hold of a call that will never report its cost.

    A call that was recorded has no hold left and this is a no-op. A provider
    refusal with a 4xx status was not billed, so its hold is released. Anything
    else — timeout, 5xx, cancellation, a success without usable usage — has an
    unknown billing outcome and keeps counting against today's limits.
    """
    if not hold_id:
        return
    try:
        from kronos.swarm_store import get_swarm

        status = getattr(error, "status_code", None)
        if error is not None and type(status) is int and 400 <= status < 500:
            get_swarm().release_budget_hold(hold_id)
        else:
            get_swarm().mark_budget_hold_unknown(hold_id)
    except Exception:  # pragma: no cover - the hold simply stays counted
        pass


@contextmanager
def model_budget_scope(force_tier: str | None = None) -> Iterator[None]:
    """Children may lower a tier, but cannot undo an outer caller's downgrade."""
    if force_tier not in (None, "lite", "standard"):
        raise ValueError("force_tier must be lite or standard")
    token = _force_lite.set(_force_lite.get() or force_tier == "lite")
    try:
        yield
    finally:
        _force_lite.reset(token)


def admit_model_call() -> bool:
    """Reserve budget for one dispatch and return the lite constraint.

    The reservation is atomic across agent processes: recorded spend plus the
    holds of calls still in flight plus this call's estimate must fit both
    limits. The hold is settled when the call's cost is recorded. The estimate
    is a policy value, not the call's price, so the bound is on how far
    concurrent calls can overshoot, not an exact cap; no provider invoice is
    reconciled here.
    """
    from kronos.security.cost_guardian import BudgetRefusedError, get_guardian

    check_execution()
    session_id = budget_session_key(get_tool_audit_context())
    guardian = get_guardian()
    try:
        if _admitted.get():
            # Inside an admitted call (a fallback attempt, a delegated lite model):
            # the day is checked again, because an earlier attempt may have been
            # billed, but no second hold is taken — the outer call's covers it.
            allowed, reason = guardian.check_budget(session_id="")
            if not allowed:
                raise ModelBudgetError(reason)
            return _force_lite.get() or guardian.should_degrade()
        allowed, reason = guardian.check_budget(session_id=session_id)
        if not allowed:
            raise ModelBudgetError(reason)
        lite = _force_lite.get() or guardian.should_degrade()
        _hold.set(guardian.reserve(session_id))
        return lite
    except ModelBudgetError:
        raise
    except BudgetRefusedError as error:
        raise ModelBudgetError(str(error)) from error
    except Exception as error:
        raise ModelBudgetError("Cost accounting unavailable; model request blocked") from error


def release_current_hold() -> None:
    """Give back this context's hold before handing the call to another budgeted model.

    A direct caller that downgrades to the factory's lite model would otherwise
    hold twice: its own reservation, never settled, and the lite model's. Inside
    an admitted call the hold belongs to the outer call and is left alone.
    """
    hold = _hold.get()
    if not hold or _admitted.get():
        return
    _hold.set("")
    try:
        from kronos.swarm_store import get_swarm

        get_swarm().release_budget_hold(hold)
    except Exception:  # pragma: no cover - the hold simply stays counted
        pass


def _release_undispatched(task: "asyncio.Future") -> None:
    if task.cancelled() or task.exception() is not None:
        return
    _, hold = task.result()
    if hold:
        try:
            from kronos.swarm_store import get_swarm

            get_swarm().release_budget_hold(hold)
        except Exception:  # pragma: no cover - the hold simply stays counted
            pass


class BudgetedModel:
    """Recheck every call and bind tools to the effective, not cached, tier.

    Only the model methods used by the runtime are exposed. An unhandled callable
    (stream/batch/structured output) must not silently escape admission through
    attribute delegation. Non-callable metadata remains available to callers.
    """

    def __init__(
        self,
        model: Any,
        *,
        label: str,
        lite_factory: Callable[[], Any],
        bindings: tuple[tuple[tuple, dict], ...] = (),
    ) -> None:
        self._model = model
        self._label = label
        self._lite_factory = lite_factory
        self._bindings = bindings

    def bind_tools(self, *args: Any, **kwargs: Any) -> "BudgetedModel":
        return BudgetedModel(
            self._model,
            label=self._label,
            lite_factory=self._lite_factory,
            bindings=(*self._bindings, (args, dict(kwargs))),
        )

    def _effective_model(self) -> Any:
        lite = admit_model_call()
        model = self._lite_factory() if lite and self._label != "lite" else self._model
        for args, kwargs in self._bindings:
            model = model.bind_tools(*args, **kwargs)
        return model

    def _admit(self) -> tuple[Any, str]:
        """Admission for a worker thread: the hold is returned, because a
        ContextVar set in the thread's copied context never reaches the caller."""
        model = self._effective_model()
        return model, current_hold()

    async def _admit_off_loop(self) -> tuple[Any, str]:
        """Reserve without blocking the loop on the shared ledger's write lock."""
        task = asyncio.ensure_future(asyncio.to_thread(self._admit))
        try:
            model, hold = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Nothing was dispatched, so a hold the worker still creates is released.
            task.add_done_callback(_release_undispatched)
            raise
        _hold.set(hold)
        return model, hold

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        if _admitted.get():
            result = self._effective_model().invoke(*args, **kwargs)
            check_execution()
            return result
        model = self._effective_model()
        hold = current_hold()
        token = _admitted.set(True)
        try:
            result = model.invoke(*args, **kwargs)
        except BaseException as error:
            finish_hold(hold, error)
            raise
        finally:
            _admitted.reset(token)
            _hold.set("")
        finish_hold(hold)
        check_execution()
        return result

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        if _admitted.get():
            result = await self._effective_model().ainvoke(*args, **kwargs)
            check_execution()
            return result
        model, hold = await self._admit_off_loop()
        token = _admitted.set(True)
        try:
            result = await model.ainvoke(*args, **kwargs)
        except BaseException as error:
            finish_hold(hold, error)
            raise
        finally:
            _admitted.reset(token)
            _hold.set("")
        finish_hold(hold)
        check_execution()
        return result

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._model, name)
        if callable(value):
            raise AttributeError(f"Model method {name} has no budget admission contract")
        return value
