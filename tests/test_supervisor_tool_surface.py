"""Registered local tools must reach the real supervisor model binding."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool

from kronos.agents import supervisor
from kronos.config import settings


@pytest.fixture
def supervisor_environment(monkeypatch, tmp_path):
    (tmp_path / "self" / "skills").mkdir(parents=True)
    monkeypatch.setattr(settings, "workspace_path", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "enable_server_ops", False)

    async def delegate(*args, **kwargs):
        raise AssertionError("this test must call a local tool directly")

    for name in (
        "create_deep_research_agent",
        "create_topic_research_agent",
        "create_knowledge_pipeline_agent",
        "create_research_agent",
        "create_task_agent",
        "create_finance_agent",
        "create_telegram_channels_agent",
        "create_analytics_agent",
        "create_competitor_monitor_agent",
    ):
        monkeypatch.setattr(supervisor, name, lambda *a, **kw: delegate)
    model = MagicMock()
    monkeypatch.setattr(supervisor, "get_orchestrator_model", lambda: model)
    return model


def make_tool(name, calls, metadata=None):
    async def execute() -> str:
        calls.append(name)
        return "local result"

    return StructuredTool.from_function(coroutine=execute, name=name, description="Test capability", metadata=metadata)


@pytest.mark.parametrize(
    "name",
    [
        "compare_offers",
        "plan_status",
        "schedule_task",
        "schedule_followup",
        "session_search",
        "convene_council",
        "ask_agent_memory",
        "open_site_session",
        "repo_search",
        "browser_snapshot",
        "import_skill_from_source",
        "new_custom_capability",
    ],
)
async def test_registered_tool_is_bound_and_can_be_called(supervisor_environment, name):
    model = supervisor_environment
    calls = []
    tool = make_tool(name, calls)
    model.bind_tools.return_value.ainvoke = AsyncMock(
        side_effect=[
            AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": "call-1"}]),
            AIMessage(content="done"),
        ]
    )
    run = supervisor.build_supervisor([tool])
    result = await run(
        [HumanMessage(content="use the registered capability")],
        request_tool_approval=lambda *_: "approval-1",
    )
    if name == "import_skill_from_source":
        assert result.waiting_approval
        assert calls == []
    else:
        assert result.content == "done"
        assert calls == [name]
    assert tool in model.bind_tools.call_args.args[0]
    assert tool in run._approval_tools


def test_custom_instance_and_metadata_are_not_replaced_by_builtin(supervisor_environment):
    custom = make_tool("add_expense", [], {"needs_approval": True, "custom_marker": "keep"})
    run = supervisor.build_supervisor([custom, custom])
    tools = run._approval_tools
    assert [t for t in tools if t.name == custom.name] == [custom]
    assert custom.metadata["custom_marker"] == "keep"
    assert len({t.name for t in tools}) == len(tools)


def test_raw_mcp_catalog_remains_delegated(supervisor_environment, monkeypatch):
    from kronos.security.mcp_tools import mark_mcp_tools

    raw = make_tool("API-get-page", [])
    mark_mcp_tools([raw], server="notion")
    factory = MagicMock(return_value=AsyncMock())
    monkeypatch.setattr(supervisor, "create_task_agent", factory)
    run = supervisor.build_supervisor([raw])
    assert raw not in run._approval_tools
    assert raw in factory.call_args.args[0]
    assert "delegate_to_task" in {tool.name for tool in run._approval_tools}


@pytest.mark.parametrize("name", ["browser_click", "browser_type", "browser_evaluate"])
def test_browser_mutations_are_approval_gated_when_exposed(name, monkeypatch):
    from kronos.engine import tool_requires_approval, tool_runs_in_parallel
    from kronos.tools.browser import tools

    monkeypatch.setattr(settings, "tool_approvals_enabled", True)
    tool = getattr(tools, name)
    assert tool_requires_approval(tool, {})
    assert not tool_runs_in_parallel(tool)
