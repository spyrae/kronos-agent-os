"""Tests for kronos.engine — custom react loop replacing LangGraph."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from kronos.config import settings
from kronos.engine import (
    AgentResult,
    create_agent,
    execute_tool,
    react_loop,
    tool_message_raw_content,
    tool_requires_approval,
)

# --- Helpers ---


def _make_tool(name: str, result: str = "tool_result", raises: Exception | None = None):
    """Create a simple async tool for testing."""

    async def _fn(**kwargs) -> str:
        if raises:
            raise raises
        return result

    return StructuredTool.from_function(
        coroutine=_fn,
        name=name,
        description=f"Test tool: {name}",
    )


def _make_model(responses: list[AIMessage]):
    """Create a mock model that returns responses in sequence.

    The model itself supports ainvoke (for no-tools case) and
    bind_tools() returns a bound version (for tools case).
    Both share the same response sequence.
    """
    model = AsyncMock()
    model.ainvoke = AsyncMock(side_effect=list(responses))

    bound = AsyncMock()
    bound.ainvoke = AsyncMock(side_effect=list(responses))
    model.bind_tools = MagicMock(return_value=bound)

    return model


def _ai_with_tool_call(tool_name: str, args: dict | None = None) -> AIMessage:
    """Create an AIMessage with a tool call."""
    return AIMessage(
        content="",
        tool_calls=[{
            "name": tool_name,
            "args": args or {},
            "id": f"call_{tool_name}_1",
        }],
    )


# --- Tests ---


class TestExecuteTool:
    """Tests for execute_tool()."""

    @pytest.mark.asyncio
    async def test_successful_execution(self):
        tool = _make_tool("search", result="found 5 results")
        tc = {"name": "search", "args": {"query": "test"}, "id": "call_1"}

        msg = await execute_tool(tool, tc)

        assert isinstance(msg, ToolMessage)
        assert msg.content == "found 5 results"
        assert msg.tool_call_id == "call_1"

    @pytest.mark.asyncio
    async def test_error_handling(self):
        tool = _make_tool("broken", raises=ConnectionError("server down"))
        tc = {"name": "broken", "args": {}, "id": "call_2"}

        msg = await execute_tool(tool, tc)

        assert isinstance(msg, ToolMessage)
        assert "[ERROR]" in msg.content
        assert msg.tool_call_id == "call_2"

    @pytest.mark.asyncio
    async def test_none_result(self):
        tool = _make_tool("void", result=None)
        tc = {"name": "void", "args": {}, "id": "call_3"}

        # Tool returns None → should become "OK"
        msg = await execute_tool(tool, tc)
        assert msg.content == "OK"

    @pytest.mark.asyncio
    async def test_to_model_output_keeps_raw_content_for_audit(self):
        async def _fn() -> list[dict]:
            return [
                {"title": "Full result", "url": "https://example.com", "text": "long raw text"},
            ]

        tool = StructuredTool.from_function(
            coroutine=_fn,
            name="heavy_tool",
            description="large output",
            metadata={"to_model_output": lambda result: f"{len(result)} result(s)"},
        )
        tc = {"name": "heavy_tool", "args": {}, "id": "call_4"}

        msg = await execute_tool(tool, tc)

        assert msg.content == "1 result(s)"
        assert tool_message_raw_content(msg) == (
            "[{'title': 'Full result', 'url': 'https://example.com', 'text': 'long raw text'}]"
        )

    @pytest.mark.asyncio
    async def test_default_heavy_tool_output_is_compacted(self):
        async def _fn() -> list[dict]:
            return [
                {
                    "title": "Result A",
                    "url": "https://example.com/a",
                    "description": "Useful details",
                },
                {
                    "title": "Result B",
                    "url": "https://example.com/b",
                    "description": "More details",
                },
            ]

        tool = StructuredTool.from_function(
            coroutine=_fn,
            name="brave_search",
            description="web search",
        )
        tc = {"name": "brave_search", "args": {}, "id": "call_5"}

        msg = await execute_tool(tool, tc)

        assert msg.content.startswith("Tool result summary for model:")
        assert "Result A — https://example.com/a" in msg.content
        assert tool_message_raw_content(msg).startswith("[{'title': 'Result A'")


class TestReactLoop:
    """Tests for react_loop()."""

    @pytest.mark.asyncio
    async def test_simple_response_no_tools(self):
        """Model responds without tool calls → single turn."""
        model = _make_model([
            AIMessage(content="Привет! Как дела?"),
        ])

        messages = [HumanMessage(content="Привет")]
        result = await react_loop(model, messages, tools=[])

        assert isinstance(result, AgentResult)
        assert result.content == "Привет! Как дела?"
        assert result.tool_calls_count == 0

    @pytest.mark.asyncio
    async def test_tool_call_then_response(self):
        """Model calls a tool, gets result, then responds."""
        tool = _make_tool("search", result="Python is a programming language")
        model = _make_model([
            _ai_with_tool_call("search", {"query": "python"}),
            AIMessage(content="Python — язык программирования."),
        ])

        messages = [HumanMessage(content="Что такое Python?")]
        result = await react_loop(model, messages, tools=[tool])

        assert result.content == "Python — язык программирования."
        assert result.tool_calls_count == 1

    @pytest.mark.asyncio
    async def test_tool_event_callback_receives_call_and_result(self):
        tool = _make_tool("search", result="Python is a programming language")
        model = _make_model([
            _ai_with_tool_call("search", {"query": "python"}),
            AIMessage(content="Python — язык программирования."),
        ])
        events = []

        messages = [HumanMessage(content="Что такое Python?")]
        result = await react_loop(
            model,
            messages,
            tools=[tool],
            on_tool_event=lambda event, payload: events.append((event, payload)),
        )

        assert result.tool_calls_count == 1
        assert [event for event, _ in events] == ["tool_call", "tool_result"]
        assert events[0][1]["name"] == "search"
        assert events[0][1]["call_id"] == "call_search_1"
        assert events[0][1]["args"] == {"query": "python"}
        assert events[1][1]["ok"] is True
        assert events[1][1]["call_id"] == "call_search_1"
        assert isinstance(events[1][1]["duration_ms"], int)

    @pytest.mark.asyncio
    async def test_tool_result_event_contains_raw_and_model_outputs(self):
        async def _fn() -> list[dict]:
            return [{"title": "Raw", "url": "https://example.com", "description": "full"}]

        tool = StructuredTool.from_function(
            coroutine=_fn,
            name="heavy_tool",
            description="large output",
            metadata={"to_model_output": lambda result: "compressed for model"},
        )
        model = _make_model([
            _ai_with_tool_call("heavy_tool"),
            AIMessage(content="done"),
        ])
        events = []

        result = await react_loop(
            model,
            [HumanMessage(content="run heavy")],
            tools=[tool],
            on_tool_event=lambda event, payload: events.append((event, payload)),
        )

        assert result.content == "done"
        tool_messages = [m for m in result.messages if isinstance(m, ToolMessage)]
        assert tool_messages[0].content == "compressed for model"
        result_event = events[1][1]
        assert result_event["content"] == (
            "[{'title': 'Raw', 'url': 'https://example.com', 'description': 'full'}]"
        )
        assert result_event["model_content"] == "compressed for model"
        assert result_event["compressed"] is True

    @pytest.mark.asyncio
    async def test_unknown_tool(self):
        """Model calls a tool that doesn't exist → error message fed back."""
        model = _make_model([
            _ai_with_tool_call("nonexistent"),
            AIMessage(content="Не удалось найти инструмент."),
        ])

        messages = [HumanMessage(content="test")]
        result = await react_loop(model, messages, tools=[])

        assert result.content == "Не удалось найти инструмент."
        assert result.tool_calls_count == 1
        # Check that error ToolMessage was created
        tool_msgs = [m for m in result.messages if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == 1
        assert "Unknown tool" in tool_msgs[0].content

    @pytest.mark.asyncio
    async def test_max_turns_exhausted(self):
        """Loop should stop after max_turns even if model keeps calling tools."""
        tool = _make_tool("loop_tool", result="ok")
        # Model always calls tools — never gives a final answer
        model = _make_model([
            _ai_with_tool_call("loop_tool") for _ in range(10)
        ])

        messages = [HumanMessage(content="infinite loop")]
        result = await react_loop(model, messages, tools=[tool], max_turns=3)

        assert "лимит итераций" in result.content.lower()
        assert result.tool_calls_count == 3

    @pytest.mark.asyncio
    async def test_system_prompt_injection(self):
        """System prompt should be passed to model but not stored in result messages."""
        model = _make_model([
            AIMessage(content="I am Kronos"),
        ])

        messages = [HumanMessage(content="who are you")]
        result = await react_loop(
            model, messages, tools=[],
            system_prompt="You are Kronos, an INTJ AI.",
        )

        assert result.content == "I am Kronos"
        # System prompt should NOT be in result messages
        from langchain_core.messages import SystemMessage
        sys_msgs = [m for m in result.messages if isinstance(m, SystemMessage)]
        assert len(sys_msgs) == 0

    @pytest.mark.asyncio
    async def test_llm_failure(self):
        """If LLM raises, return error message gracefully."""
        model = MagicMock()
        bound = AsyncMock()
        bound.ainvoke = AsyncMock(side_effect=RuntimeError("API down"))
        model.bind_tools = MagicMock(return_value=bound)

        messages = [HumanMessage(content="test")]
        result = await react_loop(model, messages, tools=[])

        assert "ошибка" in result.content.lower()

    @pytest.mark.asyncio
    async def test_tool_call_pauses_for_approval(self):
        """With approvals on (default), a risky tool pauses instead of executing."""
        calls = 0
        approval_requests = []

        async def risky_tool() -> str:
            nonlocal calls
            calls += 1
            return "executed"

        tool = StructuredTool.from_function(
            coroutine=risky_tool,
            name="mcp_add_server",
            description="mutates MCP servers",
        )
        model = _make_model([
            _ai_with_tool_call("mcp_add_server"),
            AIMessage(content="executed ok"),
        ])
        events = []

        result = await react_loop(
            model,
            [HumanMessage(content="add server")],
            tools=[tool],
            needs_tool_approval=lambda tool, args: True,
            request_tool_approval=lambda tool, tool_call: approval_requests.append(tool.name) or "apr_1",
            on_tool_event=lambda event, payload: events.append((event, payload)),
        )

        assert calls == 0  # paused, not executed
        assert approval_requests == ["mcp_add_server"]
        assert result.waiting_approval is True
        assert result.approval_id == "apr_1"
        assert result.approval_tool_name == "mcp_add_server"
        assert "Approval ID" in result.content
        assert "tool_approval_required" in [event for event, _ in events]

    @pytest.mark.asyncio
    async def test_disabling_approvals_executes_risky_tool_immediately(self, monkeypatch):
        """TOOL_APPROVALS_ENABLED=false restores immediate execution for trusted deploys."""
        monkeypatch.setattr(settings, "tool_approvals_enabled", False)
        calls = 0
        approval_requests = []

        async def risky_tool() -> str:
            nonlocal calls
            calls += 1
            return "executed"

        tool = StructuredTool.from_function(
            coroutine=risky_tool,
            name="mcp_add_server",
            description="mutates MCP servers",
        )
        model = _make_model([
            _ai_with_tool_call("mcp_add_server"),
            AIMessage(content="executed ok"),
        ])

        result = await react_loop(
            model,
            [HumanMessage(content="add server")],
            tools=[tool],
            needs_tool_approval=lambda tool, args: True,
            request_tool_approval=lambda tool, tool_call: approval_requests.append(tool.name) or "apr_1",
        )

        assert calls == 1  # executed immediately, approval bypassed
        assert approval_requests == []
        assert result.waiting_approval is False
        assert result.content == "executed ok"

    @pytest.mark.asyncio
    async def test_prior_tool_results_are_journaled_before_approval_pause(self):
        read_calls = 0
        risky_calls = 0

        async def read_tool() -> str:
            nonlocal read_calls
            read_calls += 1
            return "read ok"

        async def risky_tool() -> str:
            nonlocal risky_calls
            risky_calls += 1
            return "risky ok"

        read = StructuredTool.from_function(
            coroutine=read_tool,
            name="get_status",
            description="read status",
        )
        risky = StructuredTool.from_function(
            coroutine=risky_tool,
            name="mcp_remove_server",
            description="mutates MCP servers",
        )
        model = _make_model([
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "get_status", "args": {}, "id": "call_read"},
                    {"name": "mcp_remove_server", "args": {}, "id": "call_risky"},
                ],
            ),
            AIMessage(content="done"),
        ])
        journaled = []

        result = await react_loop(
            model,
            [HumanMessage(content="check then mutate")],
            tools=[read, risky],
            request_tool_approval=lambda tool, tool_call: "apr_multi",
            on_message_delta=lambda delta: journaled.extend(delta),
        )

        # read-only get_status runs; risky mcp_remove_server pauses for approval
        # before executing, and get_status's result is journaled before the pause.
        assert result.waiting_approval is True
        assert result.approval_tool_name == "mcp_remove_server"
        assert read_calls == 1
        assert risky_calls == 0
        tool_messages = [m for m in result.messages if isinstance(m, ToolMessage)]
        assert [(m.tool_call_id, m.content) for m in tool_messages] == [
            ("call_read", "read ok"),
        ]
        journaled_tool_messages = [m for m in journaled if isinstance(m, ToolMessage)]
        assert [m.tool_call_id for m in journaled_tool_messages] == ["call_read"]

    def test_default_approval_policy_is_enabled(self):
        risky = _make_tool("send_telegram_message")  # send_ marker → approval
        read_only = _make_tool("list_status_updates")  # list_ prefix → no approval

        assert tool_requires_approval(risky, {}) is True
        assert tool_requires_approval(read_only, {}) is False


class TestCreateAgent:
    """Tests for create_agent() factory."""

    @pytest.mark.asyncio
    async def test_agent_factory(self):
        """create_agent returns a callable that runs react_loop."""
        tool = _make_tool("calc", result="42")
        model = _make_model([
            _ai_with_tool_call("calc", {"expr": "6*7"}),
            AIMessage(content="Ответ: 42"),
        ])

        agent = create_agent(model, [tool], "You are a calculator.", name="calc_agent")

        result = await agent([HumanMessage(content="6*7")])

        assert result.content == "Ответ: 42"
        assert result.tool_calls_count == 1

    @pytest.mark.asyncio
    async def test_agent_does_not_mutate_input(self):
        """Agent should not mutate the caller's message list."""
        model = _make_model([
            AIMessage(content="ok"),
        ])

        agent = create_agent(model, [], "system", name="test")
        original = [HumanMessage(content="hi")]
        original_len = len(original)

        await agent(original)

        assert len(original) == original_len  # not mutated
