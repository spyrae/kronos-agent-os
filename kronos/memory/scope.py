"""Trusted owner of the user data a turn may read or write.

The owner is set by the runtime from the transport-verified sender (or from the
durable turn on resume/approval), never from model-supplied tool arguments.
An empty owner means "unknown": user-scoped stores must then return nothing.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_owner: ContextVar[str] = ContextVar("data_owner", default="")


@contextmanager
def data_owner_scope(user_id: str) -> Iterator[None]:
    """Bind the data owner for everything the enclosed turn runs."""
    token = _owner.set(str(user_id or "").strip())
    try:
        yield
    finally:
        _owner.reset(token)


def current_data_owner() -> str:
    """Owner bound by the runtime, or an empty string when unknown."""
    return _owner.get()
