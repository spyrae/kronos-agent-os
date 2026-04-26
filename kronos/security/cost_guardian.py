"""Cost Guardian — enforces spending limits per session and per day.

Tracks approximate costs from audit log and blocks requests
when limits are exceeded.
"""

import logging
import time
from dataclasses import dataclass, field

from kronos.audit import get_daily_cost

log = logging.getLogger("kronos.security.cost_guardian")

# Default limits (can be overridden via config)
DEFAULT_DAILY_LIMIT_USD = 5.0
DEFAULT_SESSION_LIMIT_USD = 1.0


@dataclass
class CostGuardian:
    """Tracks and enforces cost limits."""

    daily_limit: float = DEFAULT_DAILY_LIMIT_USD
    session_limit: float = DEFAULT_SESSION_LIMIT_USD

    # Per-session tracking (resets when session changes)
    _session_costs: dict[str, float] = field(default_factory=dict)

    def check_budget(self, session_id: str = "") -> tuple[bool, str]:
        """Check if request is within budget.

        Returns (allowed, reason).
        """
        # Daily limit check
        daily = get_daily_cost()
        daily_cost = daily.get("cost_usd", 0)

        if daily_cost >= self.daily_limit:
            msg = (
                f"Daily cost limit reached: ${daily_cost:.2f} / ${self.daily_limit:.2f}. "
                f"Requests: {daily.get('requests', 0)}. "
                f"Reset at midnight UTC."
            )
            log.warning("Cost guardian: %s", msg)
            return False, msg

        # Session limit check
        if session_id:
            session_cost = self._session_costs.get(session_id, 0)
            if session_cost >= self.session_limit:
                msg = (
                    f"Session cost limit reached: ${session_cost:.2f} / ${self.session_limit:.2f}. "
                    f"Start a new conversation to reset."
                )
                log.warning("Cost guardian: %s", msg)
                return False, msg

        # Warning at 80% of daily limit
        if daily_cost >= self.daily_limit * 0.8:
            log.info(
                "Cost guardian: daily budget at %.0f%% ($%.2f / $%.2f)",
                (daily_cost / self.daily_limit) * 100, daily_cost, self.daily_limit,
            )

        return True, ""

    def record_cost(self, session_id: str, cost_usd: float) -> None:
        """Record a cost for a session."""
        if session_id:
            self._session_costs[session_id] = self._session_costs.get(session_id, 0) + cost_usd

    def get_status(self) -> dict:
        """Get current cost status."""
        daily = get_daily_cost()
        return {
            "daily_cost": daily.get("cost_usd", 0),
            "daily_limit": self.daily_limit,
            "daily_requests": daily.get("requests", 0),
            "session_count": len(self._session_costs),
        }


# Singleton
_guardian: CostGuardian | None = None


def get_guardian() -> CostGuardian:
    global _guardian
    if _guardian is None:
        _guardian = CostGuardian()
    return _guardian
