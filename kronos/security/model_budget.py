"""Model-call admission, including models cached before a request starts."""

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

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        model = self._effective_model()
        hold = current_hold()
        try:
            result = model.invoke(*args, **kwargs)
        except BaseException as error:
            finish_hold(hold, error)
            raise
        finish_hold(hold)
        check_execution()
        return result

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        model = self._effective_model()
        hold = current_hold()
        try:
            result = await model.ainvoke(*args, **kwargs)
        except BaseException as error:
            finish_hold(hold, error)
            raise
        finish_hold(hold)
        check_execution()
        return result

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._model, name)
        if callable(value):
            raise AttributeError(f"Model method {name} has no budget admission contract")
        return value
