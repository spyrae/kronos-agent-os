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
    """Check current accounting before dispatch and return the lite constraint.

    This is admission against recorded spend, not an atomic money reservation.
    In-flight/unknown costs and durable session accounting need the next ledger
    stage; no exactly-once billing or hard upper bound is claimed here.
    """
    from kronos.security.cost_guardian import get_guardian

    check_execution()
    context = get_tool_audit_context()
    session_id = context.get("session_id") or context.get("thread_id", "")
    guardian = get_guardian()
    try:
        allowed, reason = guardian.check_budget(session_id=session_id)
        if not allowed:
            raise ModelBudgetError(reason)
        return _force_lite.get() or guardian.should_degrade()
    except ModelBudgetError:
        raise
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
        result = self._effective_model().invoke(*args, **kwargs)
        check_execution()
        return result

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._effective_model().ainvoke(*args, **kwargs)
        check_execution()
        return result

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._model, name)
        if callable(value):
            raise AttributeError(f"Model method {name} has no budget admission contract")
        return value
