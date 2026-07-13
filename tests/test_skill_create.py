import json

import pytest


class FakeResponse:
    def __init__(self, content: str):
        self.content = content


class FakeModel:
    def __init__(self, content: str):
        self._content = content

    def invoke(self, _messages):
        return FakeResponse(self._content)


@pytest.fixture
def skill_workspace(tmp_path, monkeypatch):
    from kronos import workspace
    from kronos.config import settings

    root = tmp_path / "workspace"
    ws = workspace.Workspace(root)
    ws.ensure_dirs()
    monkeypatch.setattr(workspace, "ws", ws)
    monkeypatch.setattr(settings, "workspace_path", str(root))
    return ws


def _complex_entries():
    return [
        {
            "ts": "2026-05-06T10:00:00+00:00",
            "session_id": "s-1",
            "input_preview": "Research a market and compare competitors",
            "output_preview": "Built a query plan and synthesized results",
            "tool_calls_count": 6,
            "supervisor_steps": 1,
        },
        {
            "ts": "2026-05-06T11:00:00+00:00",
            "session_id": "s-2",
            "input_preview": "Research another market and rank competitor signals",
            "output_preview": "Searched sources and produced a brief",
            "tool_calls_count": 5,
            "supervisor_steps": 2,
        },
    ]


@pytest.mark.asyncio
async def test_analyze_for_new_skills_creates_reviewable_draft(skill_workspace, monkeypatch):
    import kronos.cron.skill_create as skill_create

    response = """```json
{
  "found": true,
  "name": "Research Workflow",
  "description": "Reusable market research workflow",
  "trigger": "When the user asks for market or competitor research",
  "protocol": ["Plan searches", "Collect evidence", "Synthesize findings"],
  "tools": ["session_search", "brave_search"]
}
```"""
    sent = []
    monkeypatch.setattr(skill_create, "get_model", lambda _tier: FakeModel(response))
    monkeypatch.setattr(skill_create, "send_bot_api", lambda text, topic_id=None: sent.append((text, topic_id)))

    created = await skill_create.analyze_for_new_skills(entries=_complex_entries())

    assert created == "research-workflow"
    skill_file = skill_workspace.skill_path("research-workflow")
    raw = skill_file.read_text(encoding="utf-8")
    assert "status: draft" in raw
    assert "created_by: auto" in raw
    assert 'created_from_sessions: ["s-1", "s-2"]' in raw
    assert "- s-1: Research a market" in raw
    assert "- session_search" in raw
    assert sent and "Draft skill" in sent[0][0]

    manifest = json.loads((skill_workspace.skills_dir / "skills.json").read_text(encoding="utf-8"))
    row = next(skill for skill in manifest["skills"] if skill["name"] == "research-workflow")
    assert row["status"] == "draft"
    assert row["tags"] == ["auto-created", "self-improvement"]


@pytest.mark.asyncio
async def test_analyze_for_new_skills_deduplicates_existing_skill(skill_workspace, monkeypatch):
    import kronos.cron.skill_create as skill_create
    from kronos.skills.store import SkillStore

    SkillStore().add_skill(
        "research-workflow",
        "# research-workflow\n\nExisting protocol",
        {"name": "research-workflow", "description": "Reusable market research workflow"},
    )
    response = json.dumps(
        {
            "found": True,
            "name": "research-workflow",
            "description": "Reusable market research workflow",
            "trigger": "research",
            "protocol": "do research",
            "tools": [],
        }
    )
    monkeypatch.setattr(skill_create, "get_model", lambda _tier: FakeModel(response))
    monkeypatch.setattr(skill_create, "send_bot_api", lambda *_args, **_kwargs: None)

    assert await skill_create.analyze_for_new_skills(entries=_complex_entries()) is None


@pytest.mark.asyncio
async def test_analyze_for_new_skills_skips_bad_json_and_low_signal(skill_workspace, monkeypatch):
    import kronos.cron.skill_create as skill_create

    monkeypatch.setattr(skill_create, "get_model", lambda _tier: FakeModel("not json"))
    assert await skill_create.analyze_for_new_skills(entries=_complex_entries()) is None

    assert await skill_create.analyze_for_new_skills(entries=[_complex_entries()[0]]) is None


def test_approve_skill_updates_status_and_manifest(skill_workspace):
    from kronos.skills.store import SkillStore
    from kronos.skills.tools import approve_skill, init_skill_tools, load_skill

    store = SkillStore()
    store.add_skill(
        "draft-demo",
        "# draft-demo\n\nProtocol",
        {"name": "draft-demo", "description": "Draft demo", "status": "draft"},
    )
    init_skill_tools(store)

    assert "черновик" in load_skill.invoke({"skill_name": "draft-demo"})
    assert "одобрен" in approve_skill.invoke({"skill_name": "draft-demo"})
    assert "status: active" in skill_workspace.skill_path("draft-demo").read_text(encoding="utf-8")

    manifest = json.loads((skill_workspace.skills_dir / "skills.json").read_text(encoding="utf-8"))
    row = next(skill for skill in manifest["skills"] if skill["name"] == "draft-demo")
    assert row["status"] == "active"


def test_skill_improve_matches_auto_created_skill_by_metadata():
    from kronos.cron.skill_improve import _match_skill
    from kronos.skills.store import Skill

    skills = [
        Skill(
            name="research-workflow",
            description="Reusable competitive intelligence workflow",
            content="",
            path=None,
            status="draft",
        )
    ]

    assert _match_skill("Please run the competitive intelligence workflow again", skills) == "research-workflow"


@pytest.mark.asyncio
async def test_skill_improve_proposes_without_overwriting_active(skill_workspace, tmp_path, monkeypatch):
    """The weekly improver must never overwrite a live SKILL.md.

    It writes a SKILL.proposed.md next to the skill for human review; the
    active file stays byte-for-byte unchanged. This is the guard against
    persistent self-modification / prompt-injection lock-in.
    """
    from datetime import UTC, datetime, timedelta

    import kronos.cron.skill_improve as skill_improve
    import kronos.swarm_store as swarm_store
    from kronos.config import settings

    # Active skill on disk with known content.
    active = skill_workspace.skill_path("deep-research")
    active.parent.mkdir(parents=True, exist_ok=True)
    original = "# deep-research\nORIGINAL ACTIVE CONTENT — do not touch\n"
    active.write_text(original, encoding="utf-8")

    # Audit log with enough recent interactions that match "deep-research".
    # Keep the text free of other skills' keywords ("market" → investment).
    now = datetime.now(UTC)
    entries = [
        {
            "ts": (now - timedelta(hours=i)).isoformat(),
            "tier": "T1",
            "input_preview": f"research this topic deeply, iteration {i}",
        }
        for i in range(skill_improve.MIN_INTERACTIONS)
    ]
    data_dir = tmp_path / "data"
    (data_dir / "logs").mkdir(parents=True)
    (data_dir / "logs" / "audit.jsonl").write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    monkeypatch.setattr(settings, "db_path", str(data_dir / "session.db"))

    class FakeSwarm:
        def get_satisfaction_rate(self, agent_name, days):
            return {"positive": 3, "negative": 0, "satisfaction_rate": 100}

    monkeypatch.setattr(swarm_store, "get_swarm", lambda: FakeSwarm())
    monkeypatch.setattr(
        skill_improve,
        "get_model",
        lambda _tier: FakeModel("# deep-research\nPROPOSED REWRITE\n"),
    )
    sent = []
    monkeypatch.setattr(
        skill_improve,
        "send_bot_api",
        lambda text, topic_id=None: sent.append((text, topic_id)),
    )

    await skill_improve.run_skill_improve()

    # Active file untouched…
    assert active.read_text(encoding="utf-8") == original
    # …and the suggestion landed in a proposal file for review.
    proposal = active.parent / "SKILL.proposed.md"
    assert proposal.exists()
    assert "PROPOSED REWRITE" in proposal.read_text(encoding="utf-8")
    assert sent and "НЕ изменены" in sent[0][0]
