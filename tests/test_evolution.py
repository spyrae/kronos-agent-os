"""Persona evolution proposals + /persona command (roadmap 6.3)."""

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


def test_create_list_and_get(evo_env):
    from kronos import evolution

    pid = evolution.create_proposal(
        agent_name="kronos", target="soul", rationale="be warmer", proposal="Add warmth."
    )
    assert pid > 0
    pending = evolution.list_pending("kronos")
    assert len(pending) == 1
    assert pending[0]["target"] == "soul"
    assert evolution.get_proposal(pid, "kronos")["proposal"] == "Add warmth."


def test_decide_is_atomic(evo_env):
    from kronos import evolution

    pid = evolution.create_proposal(agent_name="kronos", target="soul", rationale="x", proposal="y")
    assert evolution.decide_proposal(pid, "kronos", approved=True) is not None
    # A second decision on the same proposal must not succeed.
    assert evolution.decide_proposal(pid, "kronos", approved=True) is None
    assert evolution.list_pending("kronos") == []


def test_decide_scoped_per_agent(evo_env):
    from kronos import evolution

    pid = evolution.create_proposal(agent_name="kronos", target="soul", rationale="x", proposal="y")
    assert evolution.decide_proposal(pid, "nexus", approved=True) is None  # not nexus's


def test_apply_proposal_appends_to_soul(evo_env):
    import kronos.workspace as _ws
    from kronos import evolution

    path = evolution.apply_proposal(
        {"target": "soul", "rationale": "be warmer", "proposal": "Add a warm greeting."}
    )
    content = _ws.ws.soul.read_text(encoding="utf-8")
    assert "Add a warm greeting." in content
    assert "Evolution" in content and "be warmer" in content
    assert path.endswith("SOUL.md")


def test_parse_proposal_valid_multiline():
    from kronos.cron.persona_evolve import _parse_proposal

    reply = "TARGET: soul\nRATIONALE: be more concise\nPROPOSAL: Prefer short answers.\nSecond line."
    target, rationale, proposal = _parse_proposal(reply)
    assert target == "soul"
    assert rationale == "be more concise"
    assert "Prefer short answers." in proposal and "Second line." in proposal


def test_parse_proposal_skip_and_invalid():
    from kronos.cron.persona_evolve import _parse_proposal

    assert _parse_proposal("SKIP") is None
    assert _parse_proposal("TARGET: unknown\nRATIONALE: x\nPROPOSAL: y") is None  # bad target
    assert _parse_proposal("RATIONALE: x\nPROPOSAL: y") is None  # missing target


async def test_persona_command_list_approve_flow(evo_env):
    import kronos.workspace as _ws
    from kronos import evolution
    from kronos.bridge import _handle_persona_command

    assert "Нет предложений" in await _handle_persona_command("/persona list")

    pid = evolution.create_proposal(
        agent_name="kronos", target="soul", rationale="warmer", proposal="Be warm."
    )
    assert f"#{pid}" in await _handle_persona_command("/persona")  # default = list

    result = await _handle_persona_command(f"/persona approve {pid}")
    assert "Применил" in result
    assert "Be warm." in _ws.ws.soul.read_text(encoding="utf-8")
    # Re-approving a decided proposal is rejected.
    assert "уже обработано" in await _handle_persona_command(f"/persona approve {pid}")


async def test_persona_command_reject(evo_env):
    from kronos import evolution
    from kronos.bridge import _handle_persona_command

    pid = evolution.create_proposal(
        agent_name="kronos", target="identity", rationale="x", proposal="y"
    )
    assert "Отклонил" in await _handle_persona_command(f"/persona reject {pid}")
    assert evolution.list_pending("kronos") == []


async def test_persona_command_ignores_non_persona(evo_env):
    from kronos.bridge import _handle_persona_command

    assert await _handle_persona_command("привет") is None
