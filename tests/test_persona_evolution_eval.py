"""Persona proposals arrive measured (moat phase 12.4).

The honest scope is the thing these tests pin hardest: scripted scenarios cannot
score answer quality, so the measurement is allowed to *refute* a proposal and
never to flatter one. A report that claimed a quality delta would be a number with
nothing behind it.
"""

import json

import pytest
import yaml

from kronos import evolution
from kronos.config import settings
from kronos.evolution_eval import (
    DUPLICATE_LINE_THRESHOLD,
    duplicate_ratio,
    measure_proposal,
    patched_workspace,
    prompt_delta,
    regression_pct,
    render_report,
    verdict,
)

PROPOSAL = {
    "target": "soul",
    "rationale": "Отвечать короче на простые вопросы",
    "proposal": "- Держи ответ на прямой вопрос в двух предложениях, если не просят разбор.",
}

SCENARIO = {
    "schema_version": 1,
    "name": "answers-without-tools",
    "input": "what is KAOS",
    "script": [{"content": "KAOS is the runtime you are talking to."}],
    "tool_outputs": {},
    "expect": {"max_tool_calls": 0, "must_mention": ["runtime"]},
}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A live workspace with the persona files the proposal targets."""
    import kronos.workspace as workspace_module
    from kronos.workspace import Workspace

    root = tmp_path / "workspaces" / "evo"
    self_dir = root / "self"
    self_dir.mkdir(parents=True)
    (self_dir / "SOUL.md").write_text("# Soul\n\n- Be useful.\n", encoding="utf-8")
    (self_dir / "IDENTITY.md").write_text("# Identity\n\nI am a test agent.\n", encoding="utf-8")

    original = workspace_module.ws
    workspace_module.ws = Workspace(root)
    monkeypatch.setattr(settings, "workspace_path", str(root), raising=False)
    yield root
    workspace_module.ws = original


@pytest.fixture
def suite(tmp_path):
    directory = tmp_path / "suite" / "answers-without-tools"
    directory.mkdir(parents=True)
    (directory / "scenario.yaml").write_text(yaml.safe_dump(SCENARIO, sort_keys=False), encoding="utf-8")
    return tmp_path / "suite"


@pytest.fixture
def proposals_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "data" / "session.db"))
    (tmp_path / "data").mkdir(exist_ok=True)
    import kronos.db as _db

    _db._instances.clear()
    yield
    _db._instances.clear()


# --- the patched copy ----------------------------------------------------------


def test_the_live_workspace_is_never_touched(workspace, tmp_path):
    before = (workspace / "self" / "SOUL.md").read_text(encoding="utf-8")

    patched_root = patched_workspace(PROPOSAL, into=tmp_path / "copy")

    assert (workspace / "self" / "SOUL.md").read_text(encoding="utf-8") == before
    assert "Держи ответ на прямой вопрос" in (patched_root / "self" / "SOUL.md").read_text(encoding="utf-8")


def test_the_patch_lands_on_the_named_target(workspace, tmp_path):
    patched_root = patched_workspace({**PROPOSAL, "target": "identity"}, into=tmp_path / "copy")

    assert "Держи ответ" in (patched_root / "self" / "IDENTITY.md").read_text(encoding="utf-8")
    assert "Держи ответ" not in (patched_root / "self" / "SOUL.md").read_text(encoding="utf-8")


# --- prompt assembly ----------------------------------------------------------


def test_the_prompt_delta_is_measured(workspace, tmp_path):
    patched_root = patched_workspace(PROPOSAL, into=tmp_path / "copy")

    delta = prompt_delta(PROPOSAL, patched_root)

    assert delta["built"] is True
    assert delta["chars_after"] > delta["chars_before"]
    assert delta["growth_pct"] > 0


def test_a_prompt_that_cannot_build_is_reported(workspace, tmp_path, monkeypatch):
    patched_root = patched_workspace(PROPOSAL, into=tmp_path / "copy")
    calls = {"n": 0}

    def flaky_prompt(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "current prompt"
        raise RuntimeError("template exploded")

    monkeypatch.setattr("kronos.persona.build_system_prompt", flaky_prompt)

    delta = prompt_delta(PROPOSAL, patched_root)

    assert delta["built"] is False
    assert "patched persona does not build" in delta["error"]


# --- redundancy ---------------------------------------------------------------


def test_a_proposal_already_in_the_file_is_detected(workspace):
    soul = workspace / "self" / "SOUL.md"
    soul.write_text(soul.read_text(encoding="utf-8") + PROPOSAL["proposal"] + "\n", encoding="utf-8")

    assert duplicate_ratio(PROPOSAL) == 1.0


def test_new_guidance_is_not_a_duplicate(workspace):
    assert duplicate_ratio(PROPOSAL) == 0.0


# --- the verdict --------------------------------------------------------------


def test_a_broken_prompt_is_rejected():
    report = {"regressions": [{"scenario": "system-prompt", "kind": "prompt_broken", "detail": "boom"}]}

    acceptable, reason = verdict(report, max_regression_pct=50)

    assert acceptable is False
    assert reason == "boom"


def test_a_regression_above_the_threshold_is_rejected():
    report = {
        "suite": {"scenarios": 4},
        "regressions": [{"scenario": "a", "kind": "new_failure", "detail": "failed"}],
    }

    assert regression_pct(report) == 25.0
    assert verdict(report, max_regression_pct=0)[0] is False
    assert verdict(report, max_regression_pct=50)[0] is True, "an operator may accept some noise"


def test_a_redundant_proposal_is_rejected():
    report = {"suite": {"scenarios": 4}, "regressions": [], "duplicate_ratio": DUPLICATE_LINE_THRESHOLD}

    acceptable, reason = verdict(report, max_regression_pct=0)

    assert acceptable is False
    assert "already in the file" in reason


def test_a_clean_proposal_reaches_the_owner():
    report = {"suite": {"scenarios": 4}, "regressions": [], "duplicate_ratio": 0.0}

    assert verdict(report, max_regression_pct=0) == (True, "no measurable regression")


# --- the full measurement -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_measurement_reports_what_it_cannot_prove(workspace, suite):
    report = await measure_proposal(PROPOSAL, suite_dir=suite)

    assert report["measured_quality"] is False
    assert "scripted model" in report["why_not"]
    assert report["suite"]["scenarios"] == 1
    assert report["regressions"] == []
    assert "no behaviour change in the offline suite" in report["notes"]


@pytest.mark.asyncio
async def test_a_missing_suite_leaves_the_proposal_unmeasured_not_failed(workspace, tmp_path):
    report = await measure_proposal(PROPOSAL, suite_dir=tmp_path / "absent")

    assert report["regressions"] == []
    assert any("unmeasured" in note for note in report["notes"])
    assert verdict(report, max_regression_pct=0)[0] is True, "no suite must not mean auto-reject"


@pytest.mark.asyncio
async def test_a_proposal_that_breaks_the_prompt_is_refuted(workspace, suite, monkeypatch):
    calls = {"n": 0}

    def flaky_prompt(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "current prompt"
        raise RuntimeError("template exploded")

    monkeypatch.setattr("kronos.persona.build_system_prompt", flaky_prompt)

    report = await measure_proposal(PROPOSAL, suite_dir=suite)

    assert report["regressions"][0]["kind"] == "prompt_broken"
    assert verdict(report, max_regression_pct=100)[0] is False


@pytest.mark.asyncio
async def test_the_report_renders_for_a_human(workspace, suite):
    text = render_report(await measure_proposal(PROPOSAL, suite_dir=suite))

    assert "Промпт:" in text
    assert "Сценарии:" in text
    assert "качество ответа офлайн не измеряется" in text


# --- storage and the command surface ------------------------------------------


def test_the_measurement_is_stored_with_the_proposal(proposals_db):
    measurement = {"measured_quality": False, "suite": {"scenarios": 3}, "verdict": "no measurable regression"}

    pid = evolution.create_proposal(
        agent_name="evo",
        target="soul",
        rationale="короче",
        proposal="- Короче.",
        eval_json=json.dumps(measurement),
    )

    stored = evolution.get_proposal(pid, "evo")
    assert json.loads(stored["eval_json"])["verdict"] == "no measurable regression"


def test_a_proposal_without_a_measurement_still_works(proposals_db):
    """Rows written before 12.4 have no eval_json."""
    pid = evolution.create_proposal(agent_name="evo", target="soul", rationale="r", proposal="p")

    assert evolution.get_proposal(pid, "evo")["eval_json"] in ("", None)


def test_the_rejection_reason_is_kept(proposals_db):
    pid = evolution.create_proposal(
        agent_name="evo",
        target="soul",
        rationale="r",
        proposal="p",
        eval_json=json.dumps({"suite": {"scenarios": 2}}),
    )

    evolution.decide_proposal(pid, "evo", approved=False)
    evolution.record_decision_reason(pid, "50% of scenarios regressed (a)")

    stored = json.loads(evolution.get_proposal(pid, "evo")["eval_json"])
    assert stored["decision_reason"] == "50% of scenarios regressed (a)"
    assert stored["suite"]["scenarios"] == 2, "recording a reason must not drop the measurement"


def test_rejected_proposals_stay_listable(proposals_db):
    """A measurement that hides what it rejected is one nobody can audit."""
    pid = evolution.create_proposal(agent_name="evo", target="soul", rationale="r", proposal="p")
    evolution.decide_proposal(pid, "evo", approved=False)

    assert evolution.list_pending("evo") == []
    assert [row["id"] for row in evolution.list_proposals("evo", state="rejected")] == [pid]


@pytest.mark.asyncio
async def test_persona_list_shows_the_verdict(proposals_db, monkeypatch):
    from kronos.bridge_commands import _handle_persona_command

    monkeypatch.setattr(settings, "agent_name", "evo")
    evolution.create_proposal(
        agent_name="evo",
        target="soul",
        rationale="короче отвечать",
        proposal="- Короче.",
        eval_json=json.dumps({"verdict": "no measurable regression"}),
    )

    reply = await _handle_persona_command("/persona list")

    assert "no measurable regression" in reply


@pytest.mark.asyncio
async def test_persona_show_prints_the_measurement(proposals_db, monkeypatch):
    from kronos.bridge_commands import _handle_persona_command

    monkeypatch.setattr(settings, "agent_name", "evo")
    pid = evolution.create_proposal(
        agent_name="evo",
        target="soul",
        rationale="короче отвечать",
        proposal="- Короче.",
        eval_json=json.dumps(
            {
                "measured_quality": False,
                "prompt": {"built": True, "growth_pct": 1.2, "chars_after": 4200},
                "suite": {"scenarios": 6, "base_failed": 0, "head_failed": 0},
                "entries": [],
                "notes": [],
            }
        ),
    )

    reply = await _handle_persona_command(f"/persona show {pid}")

    assert "Промпт: +1.2%" in reply
    assert "Сценарии: 6" in reply


@pytest.mark.asyncio
async def test_persona_list_rejected_shows_reasons(proposals_db, monkeypatch):
    from kronos.bridge_commands import _handle_persona_command

    monkeypatch.setattr(settings, "agent_name", "evo")
    pid = evolution.create_proposal(agent_name="evo", target="soul", rationale="r", proposal="p")
    evolution.decide_proposal(pid, "evo", approved=False)
    evolution.record_decision_reason(pid, "60% of the proposal is already in the file")

    reply = await _handle_persona_command("/persona list --rejected")

    assert "already in the file" in reply
