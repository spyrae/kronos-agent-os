"""The swarm registry as validated config (moat phase 11.1).

Two properties matter more than the schema itself: a file written before this
module still loads (the swarm is running), and a contradiction that would send
an escalation nowhere fails loudly instead of being discovered at 3am.
"""

import pytest
import yaml

from kronos.swarm_config import (
    DEFAULT_SLA_MINUTES,
    AgentProfile,
    SwarmConfigError,
    all_profiles,
    escalation_target,
    load_profiles,
    profile_for,
    profile_from_dict,
    topic_owner,
    validate_profiles,
)

LEGACY = {
    "kronos": {
        "username": "kronosagnt",
        "aliases": ["кронос"],
        "role": "strategic advisor",
    },
    "nexus": {"username": "nexusagnt", "aliases": ["нексус"], "role": "data analyst"},
}

ORGANISED = {
    "kronos": {
        "username": "kronosagnt",
        "aliases": ["кронос"],
        "role": "strategic advisor",
        "owns": ["planning", "Priorities"],
        "escalates_to": "nexus",
        "sla_minutes": 5,
        "budget_usd_daily": 1.5,
        "dissent": "require",
        "max_implicit_replies": 1,
    },
    "nexus": {
        "username": "nexusagnt",
        "aliases": ["нексус"],
        "role": "data analyst",
        "owns": ["metrics"],
        "escalates_to": "kronos",
    },
}


def _write(tmp_path, payload) -> str:
    path = tmp_path / "agents.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return str(path)


# --- backward compatibility ---------------------------------------------------


def test_a_file_without_the_new_fields_still_loads(tmp_path):
    profiles = load_profiles(_write(tmp_path, LEGACY))

    assert set(profiles) == {"kronos", "nexus"}
    kronos = profiles["kronos"]
    assert kronos.username == "kronosagnt"
    assert kronos.owns == []
    assert kronos.escalates_to == ""
    assert kronos.sla_minutes == DEFAULT_SLA_MINUTES
    assert kronos.budget_usd_daily == 0.0
    assert kronos.dissent == "allow"
    assert kronos.max_implicit_replies is None


def test_a_missing_file_is_an_empty_swarm(tmp_path):
    """The packaged distribution ships without a registry."""
    assert load_profiles(tmp_path / "absent.yaml") == {}


def test_the_router_view_keeps_the_plain_dict_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTS_CONFIG_PATH", _write(tmp_path, ORGANISED))
    from kronos.group_router import _load_profiles

    raw = _load_profiles()

    assert raw["kronos"]["username"] == "kronosagnt"
    assert raw["kronos"]["role"] == "strategic advisor"
    assert raw["kronos"]["owns"] == ["planning", "priorities"]


def test_bare_dicts_from_tests_coerce_with_defaults():
    """test_group_router injects three-key dicts straight into AGENT_PROFILES."""
    profile = profile_from_dict("operator", {"username": "opagnt", "aliases": ["op"], "role": "execution"})

    assert profile.sla_minutes == DEFAULT_SLA_MINUTES
    assert profile.owns == []


def test_an_unregistered_agent_gets_defaults():
    assert profile_for("nobody-here").sla_minutes == DEFAULT_SLA_MINUTES


def test_the_live_registry_has_a_typed_view():
    """Tools read AGENT_PROFILES as dicts; routing needs the typed fields."""
    from kronos.group_router import AGENT_PROFILES

    original = {name: dict(prof) for name, prof in AGENT_PROFILES.items()}
    AGENT_PROFILES.clear()
    AGENT_PROFILES.update({"kronos": dict(ORGANISED["kronos"]), "nexus": dict(ORGANISED["nexus"])})
    try:
        typed = all_profiles()
        assert typed["kronos"].owns == ["planning", "priorities"]
        assert profile_for("kronos").dissent == "require"
    finally:
        AGENT_PROFILES.clear()
        AGENT_PROFILES.update(original)


# --- normalisation ------------------------------------------------------------


def test_values_are_normalised_for_chat_matching(tmp_path):
    """Topics and names arrive from chat in whatever case the user typed."""
    profiles = load_profiles(
        _write(
            tmp_path,
            {"kronos": {"username": "@KronosAgnt", "aliases": ["Кронос"], "owns": ["  Planning ", ""]}},
        )
    )

    kronos = profiles["kronos"]
    assert kronos.username == "kronosagnt"
    assert kronos.aliases == ["кронос"]
    assert kronos.owns == ["planning"]
    assert kronos.owns_topic("PLANNING") is True
    assert kronos.owns_topic("") is False


def test_env_still_overrides_the_username(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_USERNAME_KRONOS", "kronos_staging")

    profiles = load_profiles(_write(tmp_path, LEGACY))

    assert profiles["kronos"].username == "kronos_staging"


def test_username_defaults_to_the_naming_pattern(tmp_path):
    profiles = load_profiles(_write(tmp_path, {"impulse": {"role": "action catalyst"}}))

    assert profiles["impulse"].username == "impulseagnt"
    assert profiles["impulse"].aliases == ["impulse"]


# --- errors -------------------------------------------------------------------


def test_escalating_to_an_unknown_agent_is_an_error(tmp_path):
    payload = {"kronos": {**LEGACY["kronos"], "escalates_to": "ghost"}}

    with pytest.raises(SwarmConfigError, match="unknown agent 'ghost'"):
        load_profiles(_write(tmp_path, payload))


def test_escalating_to_yourself_is_an_error(tmp_path):
    payload = {"kronos": {**LEGACY["kronos"], "escalates_to": "kronos"}}

    with pytest.raises(SwarmConfigError, match="escalates to itself"):
        load_profiles(_write(tmp_path, payload))


def test_an_unknown_dissent_mode_names_the_agent(tmp_path):
    payload = {"kronos": {**LEGACY["kronos"], "dissent": "maybe"}}

    with pytest.raises(SwarmConfigError, match="agent 'kronos'"):
        load_profiles(_write(tmp_path, payload))


@pytest.mark.parametrize(
    "field,value",
    [("sla_minutes", 0), ("budget_usd_daily", -1), ("max_implicit_replies", -1)],
)
def test_nonsense_numbers_are_rejected(tmp_path, field, value):
    with pytest.raises(SwarmConfigError):
        load_profiles(_write(tmp_path, {"kronos": {**LEGACY["kronos"], field: value}}))


def test_a_broken_file_is_not_silently_an_empty_swarm(tmp_path):
    path = tmp_path / "agents.yaml"
    path.write_text("kronos: [this is a list, not a profile]", encoding="utf-8")

    with pytest.raises(SwarmConfigError):
        load_profiles(str(path))


def test_a_top_level_list_is_rejected(tmp_path):
    path = tmp_path / "agents.yaml"
    path.write_text("- kronos\n- nexus\n", encoding="utf-8")

    with pytest.raises(SwarmConfigError, match="must map agent names"):
        load_profiles(str(path))


# --- warnings (legal, but worth saying out loud) ------------------------------


def test_two_owners_for_one_topic_warns_and_stays_unowned():
    profiles = {
        "kronos": AgentProfile(owns=["planning"], username="a"),
        "nexus": AgentProfile(owns=["planning"], username="b"),
    }

    warnings = validate_profiles(profiles)

    assert any("planning" in w for w in warnings)
    assert topic_owner(profiles, "planning") == "", "a contested topic must not grant the owner shortcut"


def test_over_committed_budgets_warn(monkeypatch):
    from kronos import policy as policy_module

    monkeypatch.setattr(policy_module, "_active", policy_module.Policy(budgets={"daily_usd": 2.0}))
    profiles = {
        "kronos": AgentProfile(username="a", budget_usd_daily=1.5),
        "nexus": AgentProfile(username="b", budget_usd_daily=1.5),
    }

    warnings = validate_profiles(profiles)

    assert any("above the swarm daily cap" in w for w in warnings)


def test_budgets_within_the_cap_are_quiet(monkeypatch):
    from kronos import policy as policy_module

    monkeypatch.setattr(policy_module, "_active", policy_module.Policy(budgets={"daily_usd": 5.0}))
    profiles = {"kronos": AgentProfile(username="a", budget_usd_daily=1.5)}

    assert validate_profiles(profiles) == []


# --- lookups ------------------------------------------------------------------


def test_topic_owner_and_escalation_target():
    profiles = {
        "kronos": AgentProfile(username="a", owns=["planning"], escalates_to="nexus"),
        "nexus": AgentProfile(username="b", owns=["metrics"]),
    }

    assert topic_owner(profiles, "Planning") == "kronos"
    assert topic_owner(profiles, "pricing") == ""
    assert escalation_target(profiles, "kronos") == "nexus"
    assert escalation_target(profiles, "nexus") == ""
    assert escalation_target(profiles, "ghost") == ""
