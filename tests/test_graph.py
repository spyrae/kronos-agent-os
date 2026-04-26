"""Integration-ish tests for KronosAgent.

Historical: this file tested a LangGraph StateGraph via ``build_graph``.
After the custom-engine migration (``kronos/engine.py``) that API was
removed — the agent is now the class :class:`kronos.graph.KronosAgent`.

For fine-grained contract tests of the ainvoke flow (source_kind,
persist_user_turn, extra_system_context, ephemeral peer reactions) see
:mod:`tests.test_graph_contract`. This file keeps a thin integration
layer: construction succeeds, basic init invariants hold, and the
agent exposes the expected public surface.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def workspace_dir(tmp_path):
    """Minimal workspace fixture — IDENTITY/SOUL files the prompt builder reads."""
    (tmp_path / "IDENTITY.md").write_text("# Kronos INTJ\nТы — Кронос.")
    (tmp_path / "SOUL.md").write_text("# Soul\nBe direct.")
    (tmp_path / "AGENTS.md").write_text("# Agents\n")
    (tmp_path / "methodology.md").write_text("# Methodology\n")
    skills = tmp_path / "skills"
    skills.mkdir()
    return str(tmp_path)


@pytest.fixture
def patched_settings(workspace_dir, monkeypatch, tmp_path):
    """Pin settings to an in-memory-ish test environment."""
    from kronos.config import settings

    monkeypatch.setattr(settings, "workspace_path", workspace_dir)
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "mem0_qdrant_path", str(tmp_path / "qdrant"))
    monkeypatch.setattr(settings, "fireworks_api_key", "test-key")
    monkeypatch.setattr(settings, "deepseek_api_key", "")  # keep memory disabled

    # Reset SafeDB / swarm singletons so they pick up the new paths.
    from kronos import db as _db
    _db._instances.clear()
    import kronos.swarm_store as ss
    ss._singleton = None

    return settings


class TestKronosAgentConstruction:
    def test_constructs_with_no_tools_and_no_memory(self, patched_settings):
        from kronos.graph import KronosAgent

        agent = KronosAgent(
            tools=[],
            enable_memory=False,
            enable_supervisor=False,
        )
        assert agent._session_store is None
        assert agent._supervisor is None
        assert agent._memory_enabled is False

    def test_memory_stays_disabled_without_deepseek_key(self, patched_settings):
        from kronos.graph import KronosAgent

        agent = KronosAgent(
            tools=[],
            enable_memory=True,  # requested on…
            enable_supervisor=False,
        )
        # …but DEEPSEEK_API_KEY was patched to "" → memory must refuse.
        assert agent._memory_enabled is False


class TestAinvokeContract:
    def test_signature_matches_expected_surface(self):
        """Regression guard for the ainvoke public contract.

        Callers (bridge.py, cron jobs, dashboard) rely on these exact
        keyword arguments. A rename must be done everywhere at once.
        """
        import inspect

        from kronos.graph import KronosAgent

        sig = inspect.signature(KronosAgent.ainvoke)
        assert set(sig.parameters.keys()) == {
            "self",
            "message",
            "thread_id",
            "user_id",
            "session_id",
            "source_kind",
            "persist_user_turn",
            "extra_system_context",
        }
        assert sig.parameters["source_kind"].default == "user"
        assert sig.parameters["persist_user_turn"].default is True
        assert sig.parameters["extra_system_context"].default == ""
