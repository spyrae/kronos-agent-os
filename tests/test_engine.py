"""Tests for kronos.engine — custom react loop replacing LangGraph."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from kronos.engine import (
    AgentResult,
    create_agent,
    execute_tool,
    react_loop,
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
    async def test_tool_call_waits_for_approval_before_execution(self):
        """Risky tools return a pending approval instead of executing."""
        calls = 0

        async def risky_tool() -> str:
            nonlocal calls
            calls += 1
            return "executed"

        tool = StructuredTool.from_function(
            coroutine=risky_tool,
            name="mcp_add_server",
            description="mutates MCP servers",
        )
        model = _make_model([_ai_with_tool_call("mcp_add_server")])
        events = []

        result = await react_loop(
            model,
            [HumanMessage(content="add server")],
            tools=[tool],
            request_tool_approval=lambda tool, tool_call: "apr_1",
            on_tool_event=lambda event, payload: events.append((event, payload)),
        )

        assert calls == 0
        assert result.waiting_approval is True
        assert result.approval_id == "apr_1"
        assert result.approval_tool_name == "mcp_add_server"
        assert "Approval ID" in result.content
        assert [event for event, _ in events] == ["tool_call", "tool_approval_required"]
        assert not [m for m in result.messages if isinstance(m, ToolMessage)]

    @pytest.mark.asyncio
    async def test_prior_tool_results_are_journaled_before_approval_wait(self):
        read_calls = 0
        risky_calls = 0

        async def read_tool() -> str:
            nonlocal read_calls
            read_calls += 1
            return "read ok"

        async def risky_tool() -> str:
            nonlocal risky_calls
            risky_calls += 1
            return "should wait"

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
        ])
        journaled = []

        result = await react_loop(
            model,
            [HumanMessage(content="check then mutate")],
            tools=[read, risky],
            request_tool_approval=lambda tool, tool_call: "apr_multi",
            on_message_delta=lambda delta: journaled.extend(delta),
        )

        assert result.waiting_approval is True
        assert read_calls == 1
        assert risky_calls == 0
        tool_messages = [m for m in result.messages if isinstance(m, ToolMessage)]
        assert [(m.tool_call_id, m.content) for m in tool_messages] == [
            ("call_read", "read ok"),
        ]
        journaled_tool_messages = [m for m in journaled if isinstance(m, ToolMessage)]
        assert [m.tool_call_id for m in journaled_tool_messages] == ["call_read"]

    def test_default_approval_policy_gates_writes_not_reads(self):
        risky = _make_tool("send_telegram_message")
        read_only = _make_tool("list_status_updates")

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
