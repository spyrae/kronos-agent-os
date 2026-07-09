"""Dashboard persona proposals API (roadmap 6.3 surfaced in UI)."""

import pytest

from kronos.config import settings


@pytest.fixture
def evo_env(tmp_path, monkeypatch):
    db_dir = tmp_path / "kronos"
    db_dir.mkdir(parents=True)
    monkeypatch.setattr(settings, "db_dir", str(db_dir))
    monkeypatch.setattr(settings, "db_path", str(db_dir / "session.db"))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "agent_name", "kronos")

    import kronos.db as _db

    _db._instances.clear()
    import kronos.swarm_store as ss

    ss._singleton = None

    # Isolated workspace so apply_proposal writes to a temp SOUL/IDENTITY.
    import kronos.workspace as _ws

    monkeypatch.setattr(_ws, "ws", _ws.Workspace(str(tmp_path / "workspace")))

    yield tmp_path
    _db._instances.clear()


@pytest.mark.asyncio
async def test_proposals_listed_and_rejected(evo_env):
    from dashboard.api import persona
    from kronos import evolution

    pid = evolution.create_proposal(
        agent_name="kronos", target="soul", rationale="be warmer", proposal="Add warmth."
    )

    listed = await persona.list_proposals()
    assert listed["pending"] == 1
    assert listed["proposals"][0]["id"] == pid

    result = await persona.decide_proposal(pid, persona.ProposalDecision(approved=False))
    assert result["status"] == "rejected"
    assert (await persona.list_proposals())["pending"] == 0


@pytest.mark.asyncio
async def test_proposal_approve_applies_to_workspace(evo_env):
    from dashboard.api import persona
    from kronos import evolution

    pid = evolution.create_proposal(
        agent_name="kronos", target="soul", rationale="be warmer", proposal="Add warmth."
    )

    result = await persona.decide_proposal(pid, persona.ProposalDecision(approved=True))
    assert result["status"] == "approved"

    from pathlib import Path

    applied = Path(result["applied_to"])
    assert applied.exists()
    assert "Add warmth." in applied.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_decide_unknown_proposal_404(evo_env):
    from fastapi import HTTPException

    from dashboard.api import persona

    with pytest.raises(HTTPException) as exc_info:
        await persona.decide_proposal(999, persona.ProposalDecision(approved=True))
    assert exc_info.value.status_code == 404
