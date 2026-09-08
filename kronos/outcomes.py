"""Call-local and durable execution outcomes, independent of response wording."""

from dataclasses import dataclass
from typing import Literal

InvocationStatus = Literal[
    "completed",
    "waiting_approval",
    "running",
    "blocked",
    "rejected",
    "expired",
    "failed",
    "interrupted",
    "unknown",
]


@dataclass(frozen=True)
class InvocationOutcome:
    """Execution state, not a claim that the model's answer is factually correct.

    ``turn_id`` is absent for ephemeral or input-rejected calls. Consumers must
    use this call-local value, not an agent's mutable last-approval property.
    ``unknown`` is intentionally not success: old or pruned turns may lack the
    evidence needed to reconcile work safely.
    """

    status: InvocationStatus
    content: str = ""
    thread_id: str = ""
    turn_id: str | None = None
    approval_id: str | None = None
    reason: str = ""
