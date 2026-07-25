"""Governance as code: policy.yaml loading, precedence, application (phase 9.3)."""

import pytest
import yaml

from kronos.config import settings
from kronos.policy import (
    ENV_POLICY_FILE,
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SOURCE_POLICY,
    Policy,
    PolicyError,
    activate_policy,
    apply_to_settings,
    effective_values,
    get_policy,
    load_policy,
    reset_policy,
)


@pytest.fixture(autouse=True)
def clean_policy(monkeypatch):
    """Every test starts with no policy file and no cached policy."""
    monkeypatch.delenv(ENV_POLICY_FILE, raising=False)
    reset_policy()
    yield
    reset_policy()


def _write_policy(tmp_path, payload: dict, monkeypatch) -> str:
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv(ENV_POLICY_FILE, str(path))
    return str(path)


def test_no_file_means_code_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_POLICY_FILE, str(tmp_path / "absent.yaml"))

    policy = load_policy()

    assert policy.loaded_from_file is False
    assert policy.capabilities.server_ops is False
    assert policy.approvals.enabled is True
    assert policy.budgets.daily_usd == 5.0
    assert policy.untrusted_output.on_injection == "log"


def test_policy_file_is_loaded_and_validated(tmp_path, monkeypatch):
    path = _write_policy(
        tmp_path,
        {
            "version": 1,
            "capabilities": {"server_ops": True},
            "budgets": {"daily_usd": 12.5, "per_agent_daily_usd": {"nexus": 2.0}},
            "untrusted_output": {"on_injection": "block"},
        },
        monkeypatch,
    )

    policy = load_policy()

    assert policy.source_path == path
    assert policy.capabilities.server_ops is True
    assert policy.budgets.daily_usd == 12.5
    assert policy.budgets.per_agent_daily_usd == {"nexus": 2.0}
    assert policy.untrusted_output.on_injection == "block"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"untrusted_output": {"on_injection": "panic"}}, "on_injection must be one of"),
        ({"egress": {"mode": "yolo"}}, "mode must be one of"),
        ({"budgets": {"degrade_at_fraction": 1.5}}, "degrade_at_fraction must be in"),
        ({"version": 99}, "newer than supported"),
    ],
)
def test_invalid_policy_fails_closed(tmp_path, monkeypatch, payload, message):
    _write_policy(tmp_path, payload, monkeypatch)

    with pytest.raises(PolicyError, match="is invalid"):
        load_policy()

    with pytest.raises(PolicyError) as excinfo:
        load_policy()
    assert message in str(excinfo.value)


def test_unreadable_policy_is_an_error(tmp_path, monkeypatch):
    path = tmp_path / "policy.yaml"
    path.write_text("capabilities: [this is a list, not a mapping\n", encoding="utf-8")
    monkeypatch.setenv(ENV_POLICY_FILE, str(path))

    with pytest.raises(PolicyError, match="cannot read policy"):
        load_policy()


def test_non_mapping_policy_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "policy.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    monkeypatch.setenv(ENV_POLICY_FILE, str(path))

    with pytest.raises(PolicyError, match="expected a mapping"):
        load_policy()


def test_policy_is_applied_to_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "enable_server_ops", False)
    monkeypatch.setattr(settings, "untrusted_injection_action", "log")
    _write_policy(
        tmp_path,
        {"capabilities": {"server_ops": True}, "untrusted_output": {"on_injection": "strip"}},
        monkeypatch,
    )

    changed = activate_policy()

    assert settings.enable_server_ops is True
    assert settings.untrusted_injection_action == "strip"
    assert changed.loaded_from_file is True


def test_env_override_beats_the_policy(tmp_path, monkeypatch):
    """An operator hotfix via env must not be silently ignored."""
    # Explicit env value: differs from the field default (False).
    monkeypatch.setattr(settings, "enable_server_ops", True)
    _write_policy(tmp_path, {"capabilities": {"server_ops": False}}, monkeypatch)

    policy = load_policy()
    apply_to_settings(policy)

    assert settings.enable_server_ops is True  # env wins

    rows = {row["setting"]: row for row in effective_values(policy)}
    assert rows["enable_server_ops"]["source"] == SOURCE_ENV
    assert rows["enable_server_ops"]["value"] is True


def test_report_marks_policy_and_default_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "enable_dynamic_tools", False)  # at default
    _write_policy(tmp_path, {"capabilities": {"mcp_gateway_management": True}}, monkeypatch)

    rows = {row["setting"]: row for row in effective_values(load_policy())}

    assert rows["enable_mcp_gateway_management"]["source"] == SOURCE_POLICY
    assert rows["enable_mcp_gateway_management"]["value"] is True
    assert rows["enable_dynamic_tools"]["source"] == SOURCE_POLICY  # file present
    assert rows["enable_dynamic_tools"]["value"] is False


def test_report_says_default_when_there_is_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_POLICY_FILE, str(tmp_path / "absent.yaml"))
    monkeypatch.setattr(settings, "enable_server_ops", False)

    rows = {row["setting"]: row for row in effective_values(load_policy())}

    assert rows["enable_server_ops"]["source"] == SOURCE_DEFAULT


def test_approval_lists_come_from_the_policy(tmp_path, monkeypatch):
    from langchain_core.tools import tool

    from kronos.engine import tool_requires_approval

    @tool
    def publish_report() -> str:
        """Publish a report."""
        return "ok"

    @tool
    def wiggle_widget() -> str:
        """Do something unusual."""
        return "ok"

    monkeypatch.setattr(settings, "tool_approvals_enabled", True)
    # Default rules: publish* is gated, wiggle_widget is not.
    assert tool_requires_approval(publish_report, {}) is True
    assert tool_requires_approval(wiggle_widget, {}) is False

    _write_policy(
        tmp_path, {"approvals": {"always": ["wiggle_widget"], "action_prefixes": ["frobnicate"]}}, monkeypatch
    )
    reset_policy()

    assert tool_requires_approval(wiggle_widget, {}) is True
    # The policy replaced the action prefixes, so publish is no longer gated by
    # prefix — but the name markers still catch it.
    assert tool_requires_approval(publish_report, {}) is True


def test_empty_approval_lists_keep_the_defaults(tmp_path, monkeypatch):
    """A blank YAML list is an omission, not "gate nothing"."""
    from langchain_core.tools import tool

    from kronos.engine import tool_requires_approval

    @tool
    def deploy_service() -> str:
        """Deploy."""
        return "ok"

    monkeypatch.setattr(settings, "tool_approvals_enabled", True)
    _write_policy(tmp_path, {"approvals": {"enabled": True, "always": [], "action_prefixes": []}}, monkeypatch)
    reset_policy()

    assert tool_requires_approval(deploy_service, {}) is True


def test_budgets_come_from_the_policy(tmp_path, monkeypatch):
    _write_policy(
        tmp_path, {"budgets": {"daily_usd": 0.5, "session_usd": 0.1, "degrade_at_fraction": 0.5}}, monkeypatch
    )
    reset_policy()

    from kronos.security.cost_guardian import CostGuardian

    guardian = CostGuardian()

    assert guardian.daily_limit == 0.5
    assert guardian.session_limit == 0.1


def test_get_policy_caches_and_reset_clears(tmp_path, monkeypatch):
    _write_policy(tmp_path, {"budgets": {"daily_usd": 7.0}}, monkeypatch)

    assert get_policy().budgets.daily_usd == 7.0

    _write_policy(tmp_path, {"budgets": {"daily_usd": 9.0}}, monkeypatch)
    assert get_policy().budgets.daily_usd == 7.0  # cached

    reset_policy()
    assert get_policy().budgets.daily_usd == 9.0


def test_get_policy_degrades_to_defaults_on_a_broken_file(tmp_path, monkeypatch, caplog):
    """Mid-run callers cannot crash the agent; startup is where a bad file stops it."""
    path = tmp_path / "policy.yaml"
    path.write_text("budgets:\n  daily_usd: not-a-number\n", encoding="utf-8")
    monkeypatch.setenv(ENV_POLICY_FILE, str(path))

    policy = get_policy()

    assert policy.loaded_from_file is False
    assert "Policy load failed" in caplog.text


def test_startup_refuses_to_run_with_an_invalid_policy(tmp_path, monkeypatch):
    from kronos.app import _activate_policy_or_exit

    path = tmp_path / "policy.yaml"
    path.write_text("egress:\n  mode: nonsense\n", encoding="utf-8")
    monkeypatch.setenv(ENV_POLICY_FILE, str(path))

    with pytest.raises(SystemExit) as excinfo:
        _activate_policy_or_exit()

    assert excinfo.value.code == 1


def test_example_policy_file_is_valid():
    """policy.example.yaml must load — it is what users copy."""
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "policy.example.yaml"
    policy = load_policy(example)

    assert isinstance(policy, Policy)
    assert policy.version == 1
    assert policy.capabilities.dynamic_tools is False
