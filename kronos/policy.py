"""Governance as code: one readable file describing what this agent may do.

Before this, the answer to "what is this agent allowed to do?" was spread across
six env flags, three constant lists in `engine.py`, two module constants in the
cost guardian, and an injection setting — readable only by someone willing to
grep. `policy.yaml` collects it in one place that a human (or an auditor) can
read in a minute.

Precedence is deliberate: **env > policy > code default**. The policy file is the
declared intent; an explicit environment variable is an operator overriding that
intent for one deployment, and that override must win — otherwise a hotfix via
env would silently do nothing. `kaos policy report` shows which source won for
every value, so the override is never invisible.

No file means no change: absent `policy.yaml`, everything behaves exactly as it
did before. An unreadable or invalid file fails closed at startup rather than
silently reverting to permissive defaults.
"""

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from kronos.config import Settings, settings

log = logging.getLogger("kronos.policy")

POLICY_SCHEMA_VERSION = 1
DEFAULT_POLICY_FILE = "policy.yaml"
ENV_POLICY_FILE = "KAOS_POLICY_FILE"

SOURCE_ENV = "env"
SOURCE_POLICY = "policy"
SOURCE_DEFAULT = "default"

INJECTION_ACTIONS = ("log", "strip", "block")
EGRESS_MODES = ("allowlist", "open")
TRUST_LEVELS = ("signed", "checksum", "none")
TELEMETRY_MODES = ("off", "local", "share")


class PolicyError(Exception):
    """Raised when a policy file cannot be loaded or is invalid."""


class CapabilityPolicy(BaseModel):
    """Risky runtime surfaces. Conservative by default, like the env gates."""

    dynamic_tools: bool = False
    dynamic_tool_sandbox_required: bool = True
    mcp_gateway_management: bool = False
    dynamic_mcp_servers: bool = False
    server_ops: bool = False


class ApprovalPolicy(BaseModel):
    """Which tool calls pause for a human.

    Empty lists mean "use the engine defaults" rather than "approve nothing" —
    an empty list in YAML is far more likely to be an omission than an intent to
    disable every gate.
    """

    enabled: bool = True
    always: list[str] = Field(default_factory=list)
    action_prefixes: list[str] = Field(default_factory=list)
    read_only_prefixes: list[str] = Field(default_factory=list)


class BudgetPolicy(BaseModel):
    daily_usd: float = 5.0
    session_usd: float = 1.0
    degrade_at_fraction: float = 0.8
    per_agent_daily_usd: dict[str, float] = Field(default_factory=dict)

    @field_validator("degrade_at_fraction")
    @classmethod
    def _fraction_in_range(cls, value: float) -> float:
        if not 0 < value <= 1:
            raise ValueError("degrade_at_fraction must be in (0, 1]")
        return value


class UntrustedOutputPolicy(BaseModel):
    default_for_external: bool = True
    on_injection: str = "log"

    @field_validator("on_injection")
    @classmethod
    def _known_action(cls, value: str) -> str:
        cleaned = (value or "").strip().lower()
        if cleaned not in INJECTION_ACTIONS:
            raise ValueError(f"on_injection must be one of {', '.join(INJECTION_ACTIONS)}")
        return cleaned


class EgressPolicy(BaseModel):
    mode: str = "open"
    domains: list[str] = Field(default_factory=list)
    allowed_commands: list[str] = Field(default_factory=list)
    dry_run: bool = True

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        cleaned = (value or "").strip().lower()
        if cleaned not in EGRESS_MODES:
            raise ValueError(f"mode must be one of {', '.join(EGRESS_MODES)}")
        return cleaned


class RetentionPolicy(BaseModel):
    turn_journal_days: int = 30
    audit_jsonl_days: int = 90
    swarm_messages_days: int = 30


class DurablePolicy(BaseModel):
    """How an interrupted turn is handled on the next start."""

    # "report" reproduces the historical behaviour (restore history, note the
    # interruption). "resume" re-executes the unanswered part. Default stays
    # report so an upgrade never changes what a restart does without being asked.
    resume_mode: str = "report"
    max_resume_attempts: int = 2

    @field_validator("resume_mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        cleaned = (value or "").strip().lower()
        if cleaned not in ("report", "resume"):
            raise ValueError("resume_mode must be 'report' or 'resume'")
        return cleaned


class PiiPolicy(BaseModel):
    mask_in_logs: bool = True
    mask_in_cassettes: bool = True


class RegistryPolicy(BaseModel):
    """Where skills may come from and how much they must prove (moat 12).

    `trust_default` applies to a source that declares none. `telemetry` is off by
    default and stays off unless an operator writes it here — usage data about
    which skills work is the user's, not ours.
    """

    trusted_keys: list[str] = Field(default_factory=list)
    trust_default: str = "checksum"
    require_eval_on_install: bool = True
    telemetry: str = "off"

    @field_validator("trust_default")
    @classmethod
    def _known_trust_level(cls, value: str) -> str:
        if value not in TRUST_LEVELS:
            raise ValueError(f"registry.trust_default must be one of {TRUST_LEVELS}, got {value!r}")
        return value

    @field_validator("telemetry")
    @classmethod
    def _known_telemetry_mode(cls, value: str) -> str:
        if value not in TELEMETRY_MODES:
            raise ValueError(f"registry.telemetry must be one of {TELEMETRY_MODES}, got {value!r}")
        return value


class EvolutionPolicy(BaseModel):
    """How much a self-improvement proposal must prove (moat 12.4).

    `max_regression_pct` is 0 by default: a proposal that makes any scenario fail
    is auto-rejected rather than shown to the owner.
    """

    max_regression_pct: float = 0.0
    auto_reject: bool = True

    @field_validator("max_regression_pct")
    @classmethod
    def _sane_regression(cls, value: float) -> float:
        if not 0 <= value <= 100:
            raise ValueError("evolution.max_regression_pct must be a percentage between 0 and 100")
        return value


class Policy(BaseModel):
    """The whole declared posture of one deployment."""

    version: int = POLICY_SCHEMA_VERSION
    capabilities: CapabilityPolicy = Field(default_factory=CapabilityPolicy)
    approvals: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    budgets: BudgetPolicy = Field(default_factory=BudgetPolicy)
    untrusted_output: UntrustedOutputPolicy = Field(default_factory=UntrustedOutputPolicy)
    egress: EgressPolicy = Field(default_factory=EgressPolicy)
    retention: RetentionPolicy = Field(default_factory=RetentionPolicy)
    durable: DurablePolicy = Field(default_factory=DurablePolicy)
    pii: PiiPolicy = Field(default_factory=PiiPolicy)
    registry: RegistryPolicy = Field(default_factory=RegistryPolicy)
    evolution: EvolutionPolicy = Field(default_factory=EvolutionPolicy)

    # Where this policy came from; "" means "no file, code defaults".
    source_path: str = ""

    @field_validator("version")
    @classmethod
    def _supported_version(cls, value: int) -> int:
        if value > POLICY_SCHEMA_VERSION:
            raise ValueError(f"policy version {value} is newer than supported {POLICY_SCHEMA_VERSION}")
        return value

    @property
    def loaded_from_file(self) -> bool:
        return bool(self.source_path)


# Settings fields the policy maps onto, so `apply_to_settings` and the report
# stay in sync with one table instead of two lists that drift.
_SETTINGS_MAP: tuple[tuple[str, str, str], ...] = (
    ("capabilities", "dynamic_tools", "enable_dynamic_tools"),
    ("capabilities", "dynamic_tool_sandbox_required", "require_dynamic_tool_sandbox"),
    ("capabilities", "mcp_gateway_management", "enable_mcp_gateway_management"),
    ("capabilities", "dynamic_mcp_servers", "enable_dynamic_mcp_servers"),
    ("capabilities", "server_ops", "enable_server_ops"),
    ("approvals", "enabled", "tool_approvals_enabled"),
    ("untrusted_output", "on_injection", "untrusted_injection_action"),
)


def policy_path() -> Path:
    import os

    return Path(os.environ.get(ENV_POLICY_FILE) or DEFAULT_POLICY_FILE)


def _env_override(setting_name: str) -> Any | None:
    """Return the operator's explicit value for a setting, or None.

    "Explicit" is inferred by comparing the live value with the field default:
    pydantic-settings has already merged env, .env and defaults by the time we
    look, and this is the only honest way to tell an override from a default
    without reparsing the environment.
    """
    field = Settings.model_fields.get(setting_name)
    if field is None:
        return None
    current = getattr(settings, setting_name, None)
    return current if current != field.default else None


def load_policy(path: str | Path | None = None) -> Policy:
    """Load a policy file, or return code defaults when there is none."""
    target = Path(path) if path else policy_path()
    if not target.exists():
        log.debug("No policy file at %s — using code defaults", target)
        return Policy()

    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError) as e:
        raise PolicyError(f"cannot read policy {target}: {e}") from e
    if not isinstance(raw, dict):
        raise PolicyError(f"{target}: expected a mapping at the top level")

    try:
        policy = Policy(**raw, source_path=str(target))
    except ValidationError as e:
        # Fail closed: a malformed policy must not degrade into permissive
        # defaults, the same reasoning as the webhook secret.
        raise PolicyError(f"{target} is invalid:\n{e}") from e

    log.info("Loaded policy from %s (version %d)", target, policy.version)
    return policy


def apply_to_settings(policy: Policy) -> list[str]:
    """Push policy values into settings, leaving explicit env overrides alone.

    Mutating settings (rather than teaching every call site about the policy) is
    what makes governance-as-code arrive without touching a dozen modules — the
    same approach `_force_demo_safety` already uses for demo mode. Returns the
    names of settings the policy changed, for logging and reporting.
    """
    changed: list[str] = []
    for section, field_name, setting_name in _SETTINGS_MAP:
        if _env_override(setting_name) is not None:
            continue
        value = getattr(getattr(policy, section), field_name)
        if getattr(settings, setting_name, None) != value:
            setattr(settings, setting_name, value)
            changed.append(setting_name)

    if changed:
        log.info("Policy applied to settings: %s", ", ".join(sorted(changed)))
    return changed


def effective_values(policy: Policy) -> list[dict[str, Any]]:
    """Rows of (setting, value, source) for `kaos policy report`."""
    rows: list[dict[str, Any]] = []
    for section, field_name, setting_name in _SETTINGS_MAP:
        override = _env_override(setting_name)
        policy_value = getattr(getattr(policy, section), field_name)
        if override is not None:
            source, value = SOURCE_ENV, override
        elif policy.loaded_from_file:
            source, value = SOURCE_POLICY, policy_value
        else:
            source, value = SOURCE_DEFAULT, policy_value
        rows.append(
            {
                "setting": setting_name,
                "policy_key": f"{section}.{field_name}",
                "value": value,
                "source": source,
            }
        )
    return rows


_active: Policy | None = None


def get_policy() -> Policy:
    """The active policy, loading it on first use."""
    global _active
    if _active is None:
        try:
            _active = load_policy()
        except PolicyError as e:
            # At import-adjacent call sites there is nobody to report to; startup
            # (`activate_policy`) is where a bad file must stop the process.
            log.error("Policy load failed, using defaults for this call: %s", e)
            _active = Policy()
    return _active


def activate_policy(path: str | Path | None = None) -> Policy:
    """Load, apply and remember the policy. Call once at startup."""
    global _active
    _active = load_policy(path)
    apply_to_settings(_active)
    return _active


def reset_policy() -> None:
    """Forget the cached policy (tests, and reload after a config change)."""
    global _active
    _active = None
