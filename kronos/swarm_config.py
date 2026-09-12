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

# The registry answers two questions that do not belong in the same file.
#
# *What the swarm does* — roles, ownership, escalation, budgets — is shared
# configuration: it is edited in the checkout and shipped to the host with the
# rest of the code. *Who the agents are* — the Telegram @usernames Telethon
# hands back at login — is per-installation, and a public checkout must not
# carry the handles of one private swarm.
#
# So `agents.yaml` keeps the org chart, and an optional sibling overlay carries
# the identity of this installation. The overlay is deployed-around (excluded
# from the deploy rsync) and gitignored, so it survives a deploy that rewrites
# `agents.yaml`, and never reaches the public repository.
DEFAULT_LOCAL_AGENTS_FILE = "agents.local.yaml"
ENV_LOCAL_AGENTS_FILE = "AGENTS_LOCAL_CONFIG_PATH"

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


def local_agents_file_path(base: Path) -> Path:
    """Where the per-installation overlay lives: env > sibling of the registry."""
    from_env = os.environ.get(ENV_LOCAL_AGENTS_FILE)
    if from_env:
        return Path(from_env)
    return base.parent / DEFAULT_LOCAL_AGENTS_FILE


def _read_registry(config_path: Path) -> dict[str, Any]:
    """Parse one registry file. A missing file reads as an empty mapping."""
    if not config_path.exists():
        return {}

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise SwarmConfigError(f"{config_path} is not valid YAML: {e}") from e

    if not isinstance(raw, dict):
        raise SwarmConfigError(f"{config_path} must map agent names to profiles, got {type(raw).__name__}")

    return raw


def _merge_registries(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Overlay entries win field by field, so an overlay can set only a username.

    Merging per field rather than per agent is the whole point: the overlay
    exists to correct identity, and re-stating `owns`/`escalates_to` there would
    fork the org chart into two files that drift apart.
    """
    merged: dict[str, Any] = {}
    for source in (base, overlay):
        for name, entry in source.items():
            if entry and not isinstance(entry, dict):
                raise SwarmConfigError(f"agent '{name}' must be a mapping of fields, got {type(entry).__name__}")
            merged[name] = {**merged.get(name, {}), **(entry or {})}
    return merged


def load_profiles(path: str | Path | None = None) -> dict[str, AgentProfile]:
    """Load and validate the registry, with the local overlay applied.

    A missing file yields an empty swarm (the packaged distribution ships
    without one), an unparsable or self-contradicting file raises.
    """
    config_path = agents_file_path(path)
    overlay_path = local_agents_file_path(config_path)

    raw = _read_registry(config_path)
    overlay = _read_registry(overlay_path)

    if not raw and not overlay:
        log.warning("agents.yaml not found at %s — using empty profile set", config_path)
        return {}

    if overlay:
        log.info("agents registry: %d entries overlaid from %s", len(overlay), overlay_path)

    profiles = {name: profile_from_dict(name, entry) for name, entry in _merge_registries(raw, overlay).items()}
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


def registry_username_mismatch(agent_name: str, actual_username: str | None) -> str:
    """Compare what Telegram calls this agent against what the registry claims.

    An agent never reads its own entry to recognise its own name — Telethon
    hands it the real @username at login — so a stale registry is invisible
    from the inside and stays green in every health check. It only bites the
    *other* five processes: they build their "this message is for lacuna, not
    me" index out of the registry alone, so a wrong entry means an @-address to
    that agent reads as addressed to nobody, no one skips, and the wrong agent
    answers. That is the failure this returns a string for.

    Empty string means "nothing to report" — either they agree, or Telegram
    gave no username to compare (a bot-token login, say).
    """
    actual = (actual_username or "").lower().lstrip("@")
    if not actual:
        return ""

    from kronos.group_router import AGENT_PROFILES

    entry = AGENT_PROFILES.get(agent_name)
    overlay_path = local_agents_file_path(agents_file_path())

    if entry is None:
        return (
            f"agent '{agent_name}' logged in as @{actual} but is absent from the registry — "
            f"the other agents cannot tell that @{actual} is this agent, so a message addressed "
            f"to it may be answered by someone else. Add it to {overlay_path}"
        )

    registered = (entry.get("username") or "").lower().lstrip("@")
    if registered == actual:
        return ""

    return (
        f"agent '{agent_name}' logged in as @{actual} but the registry says @{registered} — "
        f"the other agents will not recognise @{actual} as this agent, so a message addressed "
        f"to it may be answered by someone else. Fix it in {overlay_path} "
        f"(or set AGENT_USERNAME_{agent_name.upper()})"
    )
