"""Which skills are used, and what may leave the machine (moat phase 12.5).

Two properties carry this feature: the counter reflects real loads (not catalog
listings), and nothing is assembled for sharing unless the policy says so. The
second one is the test that matters — a telemetry feature is only as trustworthy
as the proof that it stays silent.
"""

import pytest

from kronos.config import settings
from kronos.skills.store import SkillStore
from kronos.skills.usage import (
    call_bucket,
    local_report,
    record_call,
    shareable_aggregate,
    telemetry_mode,
    usage,
)

SKILL_MD = """---
name: decision-memo
description: Write a one-page decision memo
version: 1.2.0
checksum: sha256:abc
eval_status: passed
---
## Steps

1. State the decision.
"""


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    skills_dir = root / "self" / "skills" / "decision-memo"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")

    plain = root / "self" / "skills" / "local-notes"
    plain.mkdir(parents=True)
    (plain / "SKILL.md").write_text("---\nname: local-notes\ndescription: Local\n---\nNotes.\n", encoding="utf-8")

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(settings, "db_dir", str(data))
    monkeypatch.setattr(settings, "db_path", str(data / "session.db"))
    monkeypatch.setattr(settings, "workspace_path", str(root), raising=False)
    import kronos.db as _db

    _db._instances.clear()
    yield root
    _db._instances.clear()


@pytest.fixture
def store(workspace):
    return SkillStore(str(workspace))


# --- the counter --------------------------------------------------------------


def test_loading_a_skill_counts_it(workspace):
    from kronos.skills.tools import init_skill_tools, load_skill

    init_skill_tools(SkillStore(str(workspace)))

    load_skill.invoke({"skill_name": "decision-memo"})
    load_skill.invoke({"skill_name": "decision-memo"})

    assert usage()["decision-memo"]["calls"] == 2
    assert usage()["decision-memo"]["last_used_at"] > 0


def test_a_missing_skill_is_not_counted(workspace):
    from kronos.skills.tools import init_skill_tools, load_skill

    init_skill_tools(SkillStore(str(workspace)))

    load_skill.invoke({"skill_name": "not-a-skill"})

    assert usage() == {}, "being asked for a skill that does not exist is not usage"


def test_listing_the_catalog_is_not_usage(store, workspace):
    """Being offered to the model is not being used."""
    store.build_catalog()

    assert usage() == {}


def test_the_counter_never_breaks_a_turn(workspace, monkeypatch):
    def broken_db():
        raise RuntimeError("database is locked")

    monkeypatch.setattr("kronos.db.get_db", lambda name: broken_db())

    record_call("decision-memo")  # must not raise


@pytest.mark.parametrize(
    "calls,expected",
    [(0, "unused"), (1, "1-9"), (9, "1-9"), (10, "10-99"), (99, "10-99"), (100, "100+"), (5000, "100+")],
)
def test_call_buckets(calls, expected):
    assert call_bucket(calls) == expected


# --- the local report ---------------------------------------------------------


def test_the_report_joins_usage_with_provenance(store, workspace):
    record_call("decision-memo")

    rows = {row["skill"]: row for row in local_report(store)}

    assert rows["decision-memo"]["calls"] == 1
    assert rows["decision-memo"]["verified"] is True
    assert rows["decision-memo"]["eval_status"] == "passed"
    assert rows["local-notes"]["calls"] == 0
    assert rows["local-notes"]["verified"] is False
    assert rows["local-notes"]["eval_status"] == "none"


def test_the_report_puts_the_used_skills_first(store, workspace):
    record_call("local-notes")

    assert [row["skill"] for row in local_report(store)] == ["local-notes", "decision-memo"]


def test_the_report_carries_no_outcome_rate(store, workspace):
    """Nothing links a turn's result to its skills, so there is no ok_rate to show."""
    record_call("decision-memo")

    row = local_report(store)[0]

    assert "ok_rate" not in row
    assert "failed" not in row


# --- sharing stays off --------------------------------------------------------


def test_telemetry_is_off_by_default():
    assert telemetry_mode() == "off"


def test_nothing_is_assembled_while_telemetry_is_off(store, workspace):
    record_call("decision-memo")

    assert shareable_aggregate(store) == []


def test_local_mode_still_shares_nothing(store, workspace, monkeypatch):
    """`local` means "count for me", not "send somewhere"."""
    from kronos import policy as policy_module

    monkeypatch.setattr(policy_module, "_active", policy_module.Policy(registry={"telemetry": "local"}))
    record_call("decision-memo")

    assert shareable_aggregate(store) == []
    assert local_report(store)[0]["calls"] == 1


def test_the_shared_payload_is_anonymous_and_coarse(store, workspace, monkeypatch):
    from kronos import policy as policy_module

    monkeypatch.setattr(policy_module, "_active", policy_module.Policy(registry={"telemetry": "share"}))
    for _ in range(12):
        record_call("decision-memo")

    payload = shareable_aggregate(store)
    row = next(item for item in payload if item["skill"] == "decision-memo")

    assert row["calls_bucket"] == "10-99", "exact counts are not shared"
    assert set(row) == {"skill", "version", "calls_bucket", "eval_status", "verified"}
    assert "last_used_at" not in row, "timestamps would describe a workflow"


def test_the_share_command_refuses_while_telemetry_is_off(workspace, capsys, monkeypatch):
    from kronos.cli import run_skills

    monkeypatch.setattr(settings, "agent_name", "usage-demo")
    monkeypatch.setattr("kronos.cli._skills_workspace_root", lambda agent: workspace)

    code = run_skills("stats", output="share")
    out = capsys.readouterr().out

    assert code == 1
    assert "nothing was sent" in out.lower()


def test_the_stats_command_says_what_it_does_not_measure(workspace, capsys, monkeypatch):
    from kronos.cli import run_skills

    monkeypatch.setattr("kronos.cli._skills_workspace_root", lambda agent: workspace)
    record_call("decision-memo")

    code = run_skills("stats")
    out = capsys.readouterr().out

    assert code == 0
    assert "decision-memo" in out
    assert "Outcomes are not tracked" in out


# --- the control room ---------------------------------------------------------


def test_the_dashboard_lists_provenance_and_usage(workspace, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "swarm_db_path", str(workspace.parent / "swarm.db"))
    record_call("decision-memo")

    from dashboard import auth
    from dashboard.server import create_app

    app = create_app()
    app.dependency_overrides[auth.verify_token] = lambda: True
    rows = {row["name"]: row for row in TestClient(app).get("/api/skills/").json()["skills"]}

    assert rows["decision-memo"]["verified"] is True
    assert rows["decision-memo"]["eval_status"] == "passed"
    assert rows["decision-memo"]["calls"] == 1
    assert rows["local-notes"]["verified"] is False


def test_yaml_bare_off_is_understood(tmp_path, monkeypatch):
    """YAML 1.1 parses `telemetry: off` as False; that must still mean off."""
    from kronos.policy import ENV_POLICY_FILE, RegistryPolicy, load_policy, reset_policy

    assert RegistryPolicy(telemetry=False).telemetry == "off"

    path = tmp_path / "policy.yaml"
    path.write_text("version: 1\nregistry:\n  telemetry: off\n", encoding="utf-8")
    monkeypatch.setenv(ENV_POLICY_FILE, str(path))
    reset_policy()
    try:
        assert load_policy().registry.telemetry == "off"
    finally:
        reset_policy()


def test_a_boolean_true_telemetry_is_refused():
    """`telemetry: on` is not a mode, and guessing which one it meant would be worse."""
    from kronos.policy import RegistryPolicy

    with pytest.raises(ValueError, match="not a boolean true"):
        RegistryPolicy(telemetry=True)
