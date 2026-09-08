"""Cooperative execution boundaries, inherited by nested tasks and pipelines."""

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

from kronos.effect_state import DurableStateError


class ExecutionStoppedError(DurableStateError):
    """The caller revoked further work; in-flight operations are not undone."""


_guard: ContextVar[Callable[[], None] | None] = ContextVar("execution_guard", default=None)


def check_execution() -> None:
    """Fail closed before starting another model, tool or pipeline action."""
    guard = _guard.get()
    if guard is not None:
        guard()


@contextmanager
def execution_scope(guard: Callable[[], None] | None) -> Iterator[None]:
    """Compose caller authority, never replace an outer caller's restrictions."""
    previous = _guard.get()

    def combined() -> None:
        if previous is not None:
            previous()
        if guard is not None:
            guard()

    token = _guard.set(combined if previous or guard else None)
    try:
        yield
    finally:
        _guard.reset(token)


class _CheckedModel:
    """Check sync custom-pipeline calls as well as the async ReAct boundary."""

    def __init__(self, model: Any):
        self._model = model

    def bind_tools(self, *args: Any, **kwargs: Any) -> "_CheckedModel":
        return _CheckedModel(self._model.bind_tools(*args, **kwargs))

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        check_execution()
        result = self._model.invoke(*args, **kwargs)
        check_execution()
        return result

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        check_execution()
        result = await self._model.ainvoke(*args, **kwargs)
        check_execution()
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._model, name)


def guard_model(model: Any) -> Any:
    """Preserve the ordinary factory API outside a caller-controlled execution."""
    return _CheckedModel(model) if _guard.get() is not None else model


_Result = TypeVar("_Result")


async def run_sync_owned(function: Callable[..., _Result], *args: Any) -> _Result:
    """Keep the caller's ownership until a dispatched worker has really exited.

    to_thread copies the execution scope. Cancelling the waiter cannot kill a
    synchronous SDK call, so wait for it instead of releasing its owner's lock.
    """

    def dispatch() -> _Result:
        check_execution()
        result = function(*args)
        check_execution()
        return result

    check_execution()
    task = asyncio.create_task(asyncio.to_thread(dispatch))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if not task.cancelled():
            task.exception()
        raise
