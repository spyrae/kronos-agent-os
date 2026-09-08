"""Durable execution failures and ownership of an external-effect intent."""

from dataclasses import dataclass


class DurableStateError(RuntimeError):
    """Execution cannot safely continue without durable state."""


class EffectUncertainError(DurableStateError):
    """An external operation may have happened; automatic replay is forbidden."""


@dataclass(frozen=True)
class EffectClaim:
    """A result to reuse, or a fenced right to perform a new effect once."""

    token: str = ""
    result: str | None = None
