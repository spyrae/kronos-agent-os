"""The supervisor honors the dashboard agent registry (agent_registry.json).

Regression guard for the split-brain where the dashboard wrote enable/disable
toggles that the supervisor never read. Opt-out semantics: only explicit
enabled=False disables; a missing/incomplete registry disables nothing.
"""

import json


def test_disabled_delegation_tool_names(monkeypatch, tmp_path):
    from kronos.agents import supervisor as sup
    from kronos.config import settings

    db_dir = tmp_path / "agent"
    db_dir.mkdir()
    monkeypatch.setattr(settings, "db_path", str(db_dir / "session.db"))

    # No registry file → nothing disabled (opt-out default).
    assert sup._disabled_delegation_tool_names() == set()

    # registry key "<name>_agent" with enabled=False → "delegate_to_<name>";
    # enabled=True is ignored; agents not listed stay enabled.
    (db_dir / "agent_registry.json").write_text(
        json.dumps(
            {
                "research_agent": {"enabled": False},
                "task_agent": {"enabled": True},
                "finance_agent": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    assert sup._disabled_delegation_tool_names() == {
        "delegate_to_research",
        "delegate_to_finance",
    }


def test_disabled_names_safe_on_malformed_registry(monkeypatch, tmp_path):
    from kronos.agents import supervisor as sup
    from kronos.config import settings

    db_dir = tmp_path / "agent"
    db_dir.mkdir()
    monkeypatch.setattr(settings, "db_path", str(db_dir / "session.db"))
    (db_dir / "agent_registry.json").write_text("{ not json", encoding="utf-8")

    # A broken registry must never disable agents — fail open.
    assert sup._disabled_delegation_tool_names() == set()
