"""MCP writes must be gated before invocation, including after hot reload."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool

from kronos.config import settings
from kronos.engine import react_loop, tool_has_side_effect, tool_requires_approval
from kronos.security.mcp_tools import mark_mcp_tools


def make_tool(name, **metadata):
    async def execute() -> str:
        return "written"

    return StructuredTool.from_function(coroutine=execute, name=name, description="Test MCP tool", metadata=metadata)


@pytest.mark.parametrize(
    "name",
    [
        "API-post-page",
        "API-patch-page",
        "API-put-block",
        "API-delete-block",
        "notion__API-post-page",
        "create_record",
        "archive_page",
        "unknown_action",
    ],
)
def test_mcp_writes_and_unknown_operations_are_gated(name, monkeypatch):
    monkeypatch.setattr(settings, "tool_approvals_enabled", True)
    tool = make_tool(name, readOnlyHint=True, needs_approval=False)
    mark_mcp_tools([tool], server="notion")
    assert tool_requires_approval(tool, {})
    assert tool_has_side_effect(tool)
    assert tool.metadata["untrusted_output"]
    assert tool.metadata["mcp_server"] == "notion"


@pytest.mark.parametrize("name", ["API-get-page", "list_pages", "read_file", "search_notes", "brave_web_search"])
def test_known_reads_do_not_require_approval(name, monkeypatch):
    monkeypatch.setattr(settings, "tool_approvals_enabled", True)
    tool = make_tool(name)
    mark_mcp_tools([tool])
    assert not tool_requires_approval(tool, {})
    assert not tool_has_side_effect(tool)


def test_explicit_side_effect_wins_over_read_name(monkeypatch):
    monkeypatch.setattr(settings, "tool_approvals_enabled", True)
    tool = make_tool("get_and_delete_record", side_effect=True)
    mark_mcp_tools([tool])
    assert tool_requires_approval(tool, {})


def test_api_write_name_is_caught_without_loader_metadata(monkeypatch):
    monkeypatch.setattr(settings, "tool_approvals_enabled", True)
    assert tool_requires_approval(make_tool("API-patch-page"), {})


async def test_notion_write_pauses_before_any_external_call(monkeypatch):
    monkeypatch.setattr(settings, "tool_approvals_enabled", True)
    calls = []

    async def execute() -> str:
        calls.append("mutated")
        return "written"

    tool = StructuredTool.from_function(coroutine=execute, name="API-post-page", description="Create page")
    mark_mcp_tools([tool], server="notion")
    response = AIMessage(content="", tool_calls=[{"name": tool.name, "args": {}, "id": "write-1"}])
    model = MagicMock()
    model.bind_tools.return_value.ainvoke = AsyncMock(return_value=response)
    result = await react_loop(
        model,
        [HumanMessage(content="Create a Notion page")],
        tools=[tool],
        request_tool_approval=lambda *_: "approval-1",
    )
    assert result.waiting_approval
    assert result.approval_id == "approval-1"
    assert calls == []


async def test_manager_applies_local_approval_rules(monkeypatch):
    from kronos.tools import manager

    tool = make_tool("API-patch-page", readOnlyHint=True)
    client = MagicMock()
    client.get_tools = AsyncMock(return_value=[tool])
    monkeypatch.setattr(manager, "MultiServerMCPClient", lambda _: client)
    loaded, error = await manager._load_server_tools("notion", {})
    assert not error
    assert loaded[0].metadata["needs_approval"] is True


async def test_gateway_start_and_reload_apply_approval_rules(monkeypatch, tmp_path):
    from kronos.tools import gateway

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "enable_dynamic_mcp_servers", False)
    monkeypatch.setattr(settings, "enable_mcp_gateway_management", True)
    monkeypatch.setattr(gateway, "build_mcp_config", lambda: {"notion": {}})
    client = MagicMock()
    first, second = make_tool("API-post-page"), make_tool("API-patch-page")
    client.get_tools = AsyncMock(side_effect=[[first], [second]])
    monkeypatch.setattr(gateway, "MultiServerMCPClient", lambda _: client)
    instance = gateway.MCPGateway()
    loaded = await instance.start()
    assert loaded[0].metadata["needs_approval"] is True
    assert "Reloaded:" in await instance.reload()
    assert instance.get_tools()[0] is second
    assert second.metadata["needs_approval"] is True
