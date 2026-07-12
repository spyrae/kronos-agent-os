"""The dashboard Skills API must write where the runtime reads.

Regression guard for the split-brain where the dashboard used
``<workspace>/skills`` while the agent's SkillStore reads
``<workspace>/self/skills`` (Workspace.skills_dir), so UI edits were invisible.
"""


def test_dashboard_skills_root_matches_runtime(monkeypatch, tmp_path):
    from dashboard.api.skills import _skills_root
    from kronos.config import settings
    from kronos.workspace import Workspace

    ws = str(tmp_path / "ws")
    monkeypatch.setattr(settings, "workspace_path", ws)

    # The dashboard's skills directory is exactly the one SkillStore loads from.
    assert _skills_root() == Workspace(ws).skills_dir
    assert _skills_root().parts[-2:] == ("self", "skills")
