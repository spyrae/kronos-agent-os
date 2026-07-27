"""Cost Guardian — enforces spending limits per session and per day.

The daily cap reads the shared swarm cost ledger (``swarm_costs``), so the
limit is swarm-wide rather than per-process; the session cap reads a
per-process tally fed by the cost-tracking callback. Blocks requests when
either limit is exceeded.

On top of those, an agent may have a personal daily slice (``budget_usd_daily``
in agents.yaml, overridable by ``budgets.per_agent_daily_usd`` in the policy).
Exhausting it does **not** block: it puts the agent in quiet mode, where it
still answers when addressed directly but stops volunteering. A hard stop would
mean the user's explicit question goes unanswered because the agent spent its
allowance on unprompted opinions, which is the wrong thing to protect.
"""

import logging
from dataclasses import dataclass, field

log = logging.getLogger("kronos.security.cost_guardian")


def _swarm_daily_cost() -> dict:
    """Swarm-wide cost totals for today (shared across all six agents).

    The daily budget is a property of the whole swarm, not of one process, so
    it reads the shared ``swarm_costs`` ledger rather than a per-agent file.
    Fails open (zeros) on any read error — a metrics glitch must not wedge an
    agent by pretending the budget is blown.
    """
    try:
        from kronos.swarm_store import get_swarm

        return get_swarm().daily_cost()
    except Exception as e:  # pragma: no cover - defensive
        log.debug("Swarm daily-cost read failed, treating as $0: %s", e)
        return {"cost_usd": 0, "requests": 0, "input_tokens": 0, "output_tokens": 0}


# Default limits (can be overridden via config)
DEFAULT_DAILY_LIMIT_USD = 5.0
DEFAULT_SESSION_LIMIT_USD = 1.0

# Once daily spend crosses this fraction of the limit, degrade to the lite tier
# (soft) instead of blocking — the hard block stays at 100%. Overridable via
# policy.budgets.degrade_at_fraction; kept as the code default.
DEGRADE_RATIO = 0.8


def _swarm_per_agent_cost() -> dict[str, float]:
    """Today's spend per agent from the shared ledger. Fails open (empty)."""
    try:
        from kronos.swarm_store import get_swarm

        return get_swarm().per_agent_daily_cost()
    except Exception as e:  # pragma: no cover - defensive
        log.debug("Swarm per-agent cost read failed, treating as $0: %s", e)
        return {}


def _policy_budgets():
    """Budget limits from the policy (falls back to the module defaults)."""
    from kronos.policy import get_policy

    return get_policy().budgets


@dataclass
class CostGuardian:
    """Tracks and enforces cost limits."""

    daily_limit: float = field(default_factory=lambda: _policy_budgets().daily_usd)
    session_limit: float = field(default_factory=lambda: _policy_budgets().session_usd)

    # Per-session tracking (resets when session changes)
    _session_costs: dict[str, float] = field(default_factory=dict)

    def check_budget(self, session_id: str = "") -> tuple[bool, str]:
        """Check if request is within budget.

        Returns (allowed, reason).
        """
        # Daily limit check
        daily = _swarm_daily_cost()
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

        # Warning at the degrade threshold
        if daily_cost >= self.daily_limit * _policy_budgets().degrade_at_fraction:
            log.info(
                "Cost guardian: daily budget at %.0f%% ($%.2f / $%.2f)",
                (daily_cost / self.daily_limit) * 100,
                daily_cost,
                self.daily_limit,
            )

        return True, ""

    def record_cost(self, session_id: str, cost_usd: float) -> None:
        """Record a cost for a session."""
        if session_id:
            self._session_costs[session_id] = self._session_costs.get(session_id, 0) + cost_usd

    def should_degrade(self) -> bool:
        """True once daily spend crosses the policy degrade fraction.

        Soft degradation: keep answering (cheaper) instead of blocking, until
        the hard daily limit in check_budget kicks in. An agent that burned most
        of its *personal* slice degrades too, even while the swarm total is
        comfortable — otherwise the first agent to wake up spends at full price
        until the shared budget is gone.
        """
        fraction = _policy_budgets().degrade_at_fraction
        daily_cost = _swarm_daily_cost().get("cost_usd", 0)
        if daily_cost >= self.daily_limit * fraction:
            return True

        personal_limit = self.personal_limit()
        return bool(personal_limit) and self.personal_spend() >= personal_limit * fraction

    # ------------------------------------------------------------------
    # Personal slice of the swarm budget (moat 11.3)
    # ------------------------------------------------------------------

    def personal_limit(self, agent: str = "") -> float:
        """This agent's own daily cap. 0 means "only the swarm cap applies".

        Precedence matches the rest of the governance stack: an explicit policy
        entry overrides what the registry declares.
        """
        from kronos.config import settings

        name = agent or settings.agent_name
        from_policy = _policy_budgets().per_agent_daily_usd.get(name)
        if from_policy:
            return float(from_policy)

        try:
            from kronos.swarm_config import profile_for

            return float(profile_for(name).budget_usd_daily)
        except Exception as e:  # pragma: no cover - defensive
            log.debug("Could not read the agent profile budget: %s", e)
            return 0.0

    def personal_spend(self, agent: str = "") -> float:
        from kronos.config import settings

        name = agent or settings.agent_name
        return float(_swarm_per_agent_cost().get(name, 0.0))

    def quiet_reason(self, agent: str = "") -> str:
        """Why this agent should stay quiet, or "" when it may volunteer.

        Quiet mode is deliberately one-way information: the router asks before
        spending a relevance call, which is where the saving is.
        """
        limit = self.personal_limit(agent)
        if limit <= 0:
            return ""
        spent = self.personal_spend(agent)
        if spent < limit:
            return ""
        return f"personal daily budget spent: ${spent:.2f} / ${limit:.2f}"

    def get_status(self) -> dict:
        """Get current cost status."""
        daily = _swarm_daily_cost()
        return {
            "daily_cost": daily.get("cost_usd", 0),
            "daily_limit": self.daily_limit,
            "daily_requests": daily.get("requests", 0),
            "session_count": len(self._session_costs),
            "personal_cost": self.personal_spend(),
            "personal_limit": self.personal_limit(),
            "quiet": bool(self.quiet_reason()),
        }


# Singleton
_guardian: CostGuardian | None = None


def get_guardian() -> CostGuardian:
    global _guardian
    if _guardian is None:
        _guardian = CostGuardian()
    return _guardian
