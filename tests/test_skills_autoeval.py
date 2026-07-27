"""A skill's own scenario, replayed at install time (moat phase 12.3).

A signature says who wrote a skill, not that it works. The scenario format from
Phase 8 is self-contained — the model's turns are scripted inside the YAML — so a
publisher can ship a check and an installer can replay it with no key and no
network. These tests pin what that check may and may not decide.
"""

import json

import pytest
import yaml

from kronos.config import settings
from kronos.skills.autoeval import (
    EVAL_ERROR,
    EVAL_FAILED,
    EVAL_MISSING,
    EVAL_PASSED,
    SCENARIO_RELPATH,
    fetch_scenario,
    name_conflicts,
    record_outcome,
    run_skill_eval,
    scenario_url,
)
from kronos.skills.registry import RegistryEntry, install
from kronos.skills.store import SkillStore

SKILL_MD = """---
name: decision-memo
description: Write a one-page decision memo
version: 1.2.0
author: publisher
---
## Steps

1. State the decision.
"""

PASSING_SCENARIO = {
    "schema_version": 1,
    "name": "memo-without-tools",
    "input": "write the memo",
    "script": [{"content": "Here is the decision memo."}],
    "tool_outputs": {},
    "expect": {"max_tool_calls": 0, "must_mention": ["decision memo"]},
}

FAILING_SCENARIO = {
    **PASSING_SCENARIO,
    "name": "memo-that-contradicts-itself",
    "expect": {"max_tool_calls": 0, "must_mention": ["quarterly budget"]},
}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    (root / "self" / "skills").mkdir(parents=True)
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(settings, "db_dir", str(data))
    return root


@pytest.fixture
def store(workspace):
    return SkillStore(str(workspace))


@pytest.fixture
def served(monkeypatch):
    """Serve SKILL.md and (optionally) a scenario, by URL suffix."""
    payloads = {"skill": SKILL_MD, "scenario": None}

    def fake_fetch(url: str, timeout: int = 15) -> str:
        if url.endswith(SCENARIO_RELPATH):
            if payloads["scenario"] is None:
                raise OSError("HTTP Error 404: Not Found")
            return payloads["scenario"]
        return payloads["skill"]

    monkeypatch.setattr("kronos.skills.hub._fetch_url", fake_fetch)
    return payloads


def _entry(**overrides) -> RegistryEntry:
    base = {
        "name": "decision-memo",
        "url": "https://example.invalid/decision-memo/SKILL.md",
        "source": "test-source",
        "trust": "none",
    }
    return RegistryEntry(**{**base, **overrides})


def _write_scenario(skill_dir, payload: dict) -> None:
    target = skill_dir / SCENARIO_RELPATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


# --- where the scenario lives --------------------------------------------------


def test_the_scenario_is_a_sibling_of_the_skill_by_convention():
    assert scenario_url("https://x.invalid/a/SKILL.md") == "https://x.invalid/a/evals/scenario.yaml"
    assert scenario_url("github:acme/skills/decision-memo") == "github:acme/skills/decision-memo/evals/scenario.yaml"
    assert scenario_url("github:acme/skills/memo/SKILL.md") == "github:acme/skills/memo/evals/scenario.yaml"


def test_an_explicit_url_wins():
    assert scenario_url("https://x.invalid/a/SKILL.md", "https://y.invalid/s.yaml") == "https://y.invalid/s.yaml"


def test_an_unrecognised_layout_yields_no_scenario_url():
    assert scenario_url("https://x.invalid/skill.txt") == ""
    assert scenario_url("") == ""


# --- fetching -----------------------------------------------------------------


def test_a_missing_scenario_is_the_normal_case(tmp_path, monkeypatch):
    def broken(url: str, timeout: int = 15) -> str:
        raise OSError("HTTP Error 404: Not Found")

    monkeypatch.setattr("kronos.skills.hub._fetch_url", broken)

    assert fetch_scenario("https://x.invalid/a/evals/scenario.yaml", tmp_path) is None


def test_an_html_error_page_from_a_guessed_url_is_not_a_scenario(tmp_path, monkeypatch):
    """Many hosts answer 200 with a page for a missing file."""
    monkeypatch.setattr("kronos.skills.hub._fetch_url", lambda url, timeout=15: "<!DOCTYPE html><html>404</html>")

    assert fetch_scenario("https://x.invalid/a/evals/scenario.yaml", tmp_path) is None
    assert not (tmp_path / SCENARIO_RELPATH).exists()


def test_garbage_from_a_declared_url_is_kept_so_it_can_be_reported(tmp_path, monkeypatch):
    """If the publisher points at a check, a broken check is their error."""
    monkeypatch.setattr("kronos.skills.hub._fetch_url", lambda url, timeout=15: "not: [a scenario")

    path = fetch_scenario("https://x.invalid/s.yaml", tmp_path, declared=True)

    assert path is not None and path.exists()


def test_a_real_scenario_is_written_next_to_the_skill(tmp_path, monkeypatch):
    monkeypatch.setattr("kronos.skills.hub._fetch_url", lambda url, timeout=15: yaml.safe_dump(PASSING_SCENARIO))

    path = fetch_scenario("https://x.invalid/a/evals/scenario.yaml", tmp_path)

    assert path == tmp_path / SCENARIO_RELPATH
    assert "memo-without-tools" in path.read_text(encoding="utf-8")


# --- replay -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_scenario_is_reported_as_missing(tmp_path):
    outcome = await run_skill_eval(tmp_path)

    assert outcome.status == EVAL_MISSING
    assert outcome.passed is False
    assert outcome.ran is False


@pytest.mark.asyncio
async def test_a_passing_scenario_passes_offline(tmp_path):
    _write_scenario(tmp_path, PASSING_SCENARIO)

    outcome = await run_skill_eval(tmp_path)

    assert outcome.status == EVAL_PASSED, outcome.detail
    assert outcome.passed is True


@pytest.mark.asyncio
async def test_a_failing_scenario_says_what_failed(tmp_path):
    _write_scenario(tmp_path, FAILING_SCENARIO)

    outcome = await run_skill_eval(tmp_path)

    assert outcome.status == EVAL_FAILED
    assert "quarterly budget" in outcome.detail


@pytest.mark.asyncio
async def test_an_unusable_scenario_is_an_error_not_a_pass(tmp_path):
    target = tmp_path / SCENARIO_RELPATH
    target.parent.mkdir(parents=True)
    target.write_text("name: broken\n", encoding="utf-8")  # no script to replay

    outcome = await run_skill_eval(tmp_path)

    assert outcome.status == EVAL_ERROR
    assert outcome.passed is False


# --- the verdict is recorded ---------------------------------------------------


def test_the_verdict_lands_in_frontmatter_without_touching_the_checksum(store, workspace):
    from kronos.skills.autoeval import EvalOutcome
    from kronos.skills.integrity import compute_checksum

    skill_dir = workspace / "self" / "skills" / "decision-memo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    fresh = SkillStore(str(workspace))
    before = compute_checksum(skill_dir)

    record_outcome(fresh, "decision-memo", EvalOutcome(EVAL_PASSED, "scenario 'x' passed"))

    assert compute_checksum(skill_dir) == before, "a local verdict must not invalidate a published checksum"
    reloaded = SkillStore(str(workspace)).get("decision-memo")
    assert reloaded.eval_status == EVAL_PASSED
    assert "passed" in reloaded.eval_detail


def test_the_manifest_exposes_verified_signed_and_eval_status(store, workspace):
    skill_dir = workspace / "self" / "skills" / "decision-memo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        SKILL_MD.replace("author: publisher", "author: publisher\nchecksum: sha256:abc\neval_status: passed"),
        encoding="utf-8",
    )

    manifest = SkillStore(str(workspace)).generate_manifest()
    row = next(entry for entry in manifest["skills"] if entry["name"] == "decision-memo")

    assert row["verified"] is True
    assert row["signed"] is False
    assert row["eval_status"] == "passed"


# --- name conflicts -----------------------------------------------------------


def test_an_installed_name_is_a_conflict(store, workspace):
    skill_dir = workspace / "self" / "skills" / "decision-memo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")

    reason = name_conflicts("decision-memo", SkillStore(str(workspace)))

    assert "already installed" in reason


def test_a_tool_name_is_a_conflict(store):
    """A skill shadowing a tool would make load_skill and the router disagree."""
    reason = name_conflicts("schedule_task", store, tool_names=["schedule_task"])

    assert "name of an existing tool" in reason


def test_the_built_in_tool_names_are_discoverable(store):
    from kronos.skills.autoeval import known_tool_names

    names = known_tool_names()

    assert "schedule_task" in names, "the inventory must actually see the built-in tools"
    assert name_conflicts("schedule_task", store) != ""


def test_a_free_name_has_no_conflict(store):
    assert name_conflicts("brand-new-skill", store, tool_names=["schedule_task"]) == ""


# --- install gating -----------------------------------------------------------


def test_a_skill_with_a_passing_scenario_records_it(store, served):
    served["scenario"] = yaml.safe_dump(PASSING_SCENARIO)

    result = install("decision-memo", store=store, entries=[_entry()])

    assert result.installed is True
    assert result.report["eval_status"] == EVAL_PASSED
    assert store.get("decision-memo").eval_status == EVAL_PASSED


def test_a_failing_scenario_keeps_the_skill_a_draft(store, served):
    served["scenario"] = yaml.safe_dump(FAILING_SCENARIO)

    result = install("decision-memo", store=store, entries=[_entry()])

    assert result.status == "draft"
    assert "quarterly budget" in result.reason
    assert store.get("decision-memo").eval_status == EVAL_FAILED


def test_a_skill_without_a_scenario_is_marked_unverified(store, served):
    result = install("decision-memo", store=store, entries=[_entry()])

    assert result.installed is True
    assert result.report["eval_status"] == EVAL_MISSING
    assert "unverified" in result.reason


def test_no_eval_flag_skips_the_check_entirely(store, served, monkeypatch):
    served["scenario"] = yaml.safe_dump(FAILING_SCENARIO)
    calls = {"n": 0}

    def counted(skill_dir):
        calls["n"] += 1
        raise AssertionError("must not run")

    monkeypatch.setattr("kronos.skills.autoeval.run_skill_eval", counted)

    result = install("decision-memo", store=store, entries=[_entry()], run_eval=False)

    assert result.installed is True
    assert calls["n"] == 0


def test_a_name_conflict_is_refused_before_fetching(store, workspace, monkeypatch):
    skill_dir = workspace / "self" / "skills" / "taken"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_MD.replace("decision-memo", "taken"), encoding="utf-8")
    fresh = SkillStore(str(workspace))

    def must_not_fetch(url: str, timeout: int = 15) -> str:
        raise AssertionError("install must not fetch a name it cannot use")

    monkeypatch.setattr("kronos.skills.hub._fetch_url", must_not_fetch)

    result = install("taken", store=fresh, entries=[_entry(name="taken")])

    assert result.installed is False
    assert "already installed" in result.reason


def test_the_index_can_point_at_a_scenario_elsewhere(store, monkeypatch):
    fetched: list[str] = []

    def fake_fetch(url: str, timeout: int = 15) -> str:
        fetched.append(url)
        if url == "https://elsewhere.invalid/checks/memo.yaml":
            return yaml.safe_dump(PASSING_SCENARIO)
        return SKILL_MD

    monkeypatch.setattr("kronos.skills.hub._fetch_url", fake_fetch)

    result = install(
        "decision-memo",
        store=store,
        entries=[_entry(scenario_url="https://elsewhere.invalid/checks/memo.yaml")],
    )

    assert "https://elsewhere.invalid/checks/memo.yaml" in fetched
    assert result.report["eval_status"] == EVAL_PASSED


def test_the_policy_can_stop_the_eval_from_gating(store, served, monkeypatch):
    """An operator may want the verdict recorded but not enforced."""
    from kronos import policy as policy_module

    served["scenario"] = yaml.safe_dump(FAILING_SCENARIO)
    monkeypatch.setattr(
        policy_module,
        "_active",
        policy_module.Policy(registry={"require_eval_on_install": False}),
    )

    result = install("decision-memo", store=store, entries=[_entry()])

    # Still a draft here (nothing signed it), but the failure is not the reason
    # activation was withheld — the recorded verdict proves the check ran.
    assert result.report["eval_status"] == EVAL_FAILED
    assert json.dumps(result.report)  # report stays serialisable for the dashboard
