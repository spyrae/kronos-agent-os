"""The swarm's org chart as a validated config file.

`agents.yaml` used to answer one question: which @username belongs to which
agent. That is enough to stop two agents from answering the same message, but
not enough to make them a team — nobody owns a topic, silence has no
consequence, and one agent can spend the whole swarm's daily budget.

This module extends the same file with the organisational facts:

* `owns` — topics where this agent answers without waiting for a relevance
  score, and wins arbitration against agents who merely find the topic
  interesting.
* `escalates_to` — who picks the topic up when the owner stays silent.
* `sla_minutes` — how long "silent" is.
* `budget_usd_daily` — the agent's own slice of the swarm budget.
* `dissent` — whether a final answer here needs a challenge from another role.
* `max_implicit_replies` — per-agent override of the global implicit-reply cap.

**Absent fields keep today's behaviour.** A file written before this module
loads with defaults that make the extended routing a no-op, which is what makes
this safe to ship to a running swarm.

Validation is deliberately split. A broken `escalates_to` is an error: it names
a delivery path that does not exist, so failing loudly beats routing an
escalation into the void. Overlapping ownership and over-committed budgets are
warnings: both are legitimate transitional states (two agents sharing a topic
during a handover; a budget sum above the cap when the cap is about to be
raised), and refusing to start would be worse than saying so in the log.
"""

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

log = logging.getLogger("kronos.swarm_config")

DEFAULT_AGENTS_FILE = "agents.yaml"
ENV_AGENTS_FILE = "AGENTS_CONFIG_PATH"

DISSENT_MODES = ("allow", "require")

# Owner-first routing defaults. 15 minutes is short enough that a silent owner
# does not strand the user, long enough that a non-owner does not answer over
# the specialist while they are still typing.
DEFAULT_SLA_MINUTES = 15


class SwarmConfigError(Exception):
    """Raised when agents.yaml cannot be loaded or contradicts itself."""


class AgentProfile(BaseModel):
    """One agent's entry in the swarm registry."""

    username: str = ""
    aliases: list[str] = Field(default_factory=list)
    role: str = ""

    owns: list[str] = Field(default_factory=list)
    escalates_to: str = ""
    sla_minutes: int = DEFAULT_SLA_MINUTES
    # 0 means "no personal cap" — the agent is bounded only by the swarm budget.
    budget_usd_daily: float = 0.0
    dissent: str = "allow"
    # None means "use the router's global cap" rather than "no replies".
    max_implicit_replies: int | None = None

    @field_validator("username")
    @classmethod
    def _normalise_username(cls, value: str) -> str:
        return value.lower().lstrip("@")

    @field_validator("aliases")
    @classmethod
    def _normalise_aliases(cls, value: list[str]) -> list[str]:
        return [alias.lower() for alias in value]

    @field_validator("owns")
    @classmethod
    def _normalise_topics(cls, value: list[str]) -> list[str]:
        return [topic.strip().lower() for topic in value if topic.strip()]

    @field_validator("dissent")
    @classmethod
    def _known_dissent_mode(cls, value: str) -> str:
        if value not in DISSENT_MODES:
            raise ValueError(f"dissent must be one of {DISSENT_MODES}, got {value!r}")
        return value

    @field_validator("sla_minutes")
    @classmethod
    def _positive_sla(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("sla_minutes must be positive")
        return value

    @field_validator("budget_usd_daily")
    @classmethod
    def _non_negative_budget(cls, value: float) -> float:
        if value < 0:
            raise ValueError("budget_usd_daily cannot be negative")
        return value

    @field_validator("max_implicit_replies")
    @classmethod
    def _non_negative_cap(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("max_implicit_replies cannot be negative")
        return value

    def owns_topic(self, topic: str) -> bool:
        """Case-insensitive membership — topics arrive from chat, not from code."""
        return bool(topic) and topic.strip().lower() in self.owns


def profile_from_dict(name: str, raw: dict[str, Any]) -> AgentProfile:
    """Coerce one registry entry into a profile, applying env overrides.

    Tolerates entries that predate the extended schema (and the bare dicts that
    tests inject into ``AGENT_PROFILES``) — every new field has a default.
    """
    if raw and not isinstance(raw, dict):
        raise SwarmConfigError(f"agent '{name}' must be a mapping of fields, got {type(raw).__name__}")
    data = dict(raw or {})
    username = os.environ.get(
        f"AGENT_USERNAME_{name.upper()}",
        data.get("username") or f"{name}agnt",
    )
    data["username"] = username
    if not data.get("aliases"):
        data["aliases"] = [name]
    try:
        return AgentProfile(**data)
    except ValidationError as e:
        raise SwarmConfigError(f"agent '{name}' has an invalid profile: {e}") from e


def agents_file_path(path: str | Path | None = None) -> Path:
    """Where the registry lives: explicit argument > env > package-relative."""
    if path is not None:
        return Path(path)
    from_env = os.environ.get(ENV_AGENTS_FILE)
    if from_env:
        return Path(from_env)
    return (Path(__file__).resolve().parent.parent / DEFAULT_AGENTS_FILE).resolve()


def load_profiles(path: str | Path | None = None) -> dict[str, AgentProfile]:
    """Load and validate the registry.

    A missing file yields an empty swarm (the packaged distribution ships
    without one), an unparsable or self-contradicting file raises.
    """
    config_path = agents_file_path(path)

    if not config_path.exists():
        log.warning("agents.yaml not found at %s — using empty profile set", config_path)
        return {}

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise SwarmConfigError(f"{config_path} is not valid YAML: {e}") from e

    if not isinstance(raw, dict):
        raise SwarmConfigError(f"{config_path} must map agent names to profiles, got {type(raw).__name__}")

    profiles = {name: profile_from_dict(name, entry) for name, entry in raw.items()}
    validate_profiles(profiles)
    return profiles


def validate_profiles(profiles: dict[str, AgentProfile]) -> list[str]:
    """Check cross-agent consistency. Raises on errors, returns warnings."""
    for name, profile in profiles.items():
        target = profile.escalates_to
        if not target:
            continue
        if target == name:
            raise SwarmConfigError(f"agent '{name}' escalates to itself — escalation would never leave the agent")
        if target not in profiles:
            known = ", ".join(sorted(profiles)) or "none"
            raise SwarmConfigError(f"agent '{name}' escalates to unknown agent '{target}' (known agents: {known})")

    warnings = _ownership_warnings(profiles) + _budget_warnings(profiles)
    for warning in warnings:
        log.warning("agents.yaml: %s", warning)
    return warnings


def _ownership_warnings(profiles: dict[str, AgentProfile]) -> list[str]:
    """Two owners for one topic is legal but ambiguous — say so once per topic."""
    owners: dict[str, list[str]] = {}
    for name, profile in profiles.items():
        for topic in profile.owns:
            owners.setdefault(topic, []).append(name)

    return [
        f"topic '{topic}' is owned by {', '.join(sorted(names))} — arbitration between them falls back to relevance"
        for topic, names in sorted(owners.items())
        if len(names) > 1
    ]


def _budget_warnings(profiles: dict[str, AgentProfile]) -> list[str]:
    """Per-agent budgets summing above the swarm cap cannot all be spent."""
    committed = sum(profile.budget_usd_daily for profile in profiles.values())
    if committed <= 0:
        return []
    try:
        from kronos.policy import get_policy

        swarm_limit = get_policy().budgets.daily_usd
    except Exception as e:  # pragma: no cover - policy is optional at load time
        log.debug("Could not read the swarm budget while validating agents.yaml: %s", e)
        return []

    if committed > swarm_limit:
        return [
            f"per-agent budgets total ${committed:.2f}, above the swarm daily cap ${swarm_limit:.2f} — "
            f"the swarm limit will bind first"
        ]
    return []


def all_profiles() -> dict[str, AgentProfile]:
    """Extended view of the live registry.

    `group_router.AGENT_PROFILES` stays a dict of plain dicts — tools index it
    by key and tests replace its contents wholesale — so the typed view is
    derived on demand instead of becoming a second source of truth.
    """
    from kronos.group_router import AGENT_PROFILES

    return {name: profile_from_dict(name, raw) for name, raw in AGENT_PROFILES.items()}


def profile_for(agent_name: str) -> AgentProfile:
    """Extended profile for one agent (defaults when it is not registered)."""
    from kronos.group_router import AGENT_PROFILES

    return profile_from_dict(agent_name, AGENT_PROFILES.get(agent_name, {}))


def escalation_target(profiles: dict[str, AgentProfile], agent_name: str) -> str:
    """Who covers for this agent. Empty string means "nobody"."""
    profile = profiles.get(agent_name)
    return profile.escalates_to if profile else ""


def topic_owner(profiles: dict[str, AgentProfile], topic: str) -> str:
    """The single owner of a topic, or "" when unowned or contested.

    Contested topics deliberately return "" — the ownership shortcut only makes
    sense when it points at one agent, and `validate_profiles` already warned.
    """
    if not topic:
        return ""
    owners = [name for name, profile in profiles.items() if profile.owns_topic(topic)]
    return owners[0] if len(owners) == 1 else ""
