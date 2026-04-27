"""Custom agent engine — replaces LangGraph's create_react_agent + ToolNode.

Provides:
- execute_tool(): run a single tool with error handling
- react_loop(): LLM ↔ tool execution loop (the core ReAct pattern)
- create_agent(): factory that returns a reusable agent function

No LangGraph dependency. Uses langchain_core messages and tools directly.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool

from kronos.tools.error_handler import classify_tool_error

log = logging.getLogger("kronos.engine")

MAX_REACT_TURNS = 25
TOOL_TIMEOUT_SECONDS = 120
ToolEventCallback = Callable[[str, dict[str, Any]], None]


@dataclass
class AgentResult:
    """Result of an agent execution."""

    messages: list[BaseMessage]
    content: str  # final text response
    tool_calls_count: int = 0


async def execute_tool(
    tool: BaseTool,
    tool_call: dict,
    error_handler: Callable[[Exception], str] = classify_tool_error,
) -> ToolMessage:
    """Execute a single tool call, returning a ToolMessage.

    Handles both sync and async tools. On error, calls error_handler
    to produce an actionable message for the LLM.
    """
    tool_call_id = tool_call.get("id", "")
    args = tool_call.get("args", {})

    try:
        if hasattr(tool, "ainvoke"):
            result = await asyncio.wait_for(
                tool.ainvoke(args),
                timeout=TOOL_TIMEOUT_SECONDS,
            )
        else:
            result = tool.invoke(args)

        content = str(result) if result is not None else "OK"

    except TimeoutError:
        content = f"[ERROR] Tool '{tool.name}' timed out after {TOOL_TIMEOUT_SECONDS}s"
        log.error("Tool timeout: %s", tool.name)
    except Exception as e:
        content = error_handler(e)
        log.warning("Tool error %s: %s", tool.name, str(e)[:200])

    return ToolMessage(content=content, tool_call_id=tool_call_id)


async def react_loop(
    model: BaseChatModel,
    messages: list[BaseMessage],
    tools: list[BaseTool],
    system_prompt: str | None = None,
    max_turns: int = MAX_REACT_TURNS,
    error_handler: Callable[[Exception], str] = classify_tool_error,
    on_tool_event: ToolEventCallback | None = None,
) -> AgentResult:
    """Run the ReAct loop: LLM → tool_calls → execute → LLM → ...

    Args:
        model: LLM with tool-calling support.
        messages: Conversation history (will be mutated with new messages).
        tools: Available tools for this loop.
        system_prompt: Optional system prompt prepended to messages.
        max_turns: Max LLM calls before forced stop.
        error_handler: Callback for tool execution errors.

    Returns:
        AgentResult with full message history and final text content.
    """
    # Build tool lookup
    tool_map: dict[str, BaseTool] = {t.name: t for t in tools}

    # Bind tools to model (skip if no tools — some models don't support empty tool lists)
    if tools:
        bound_model = model.bind_tools(tools)
    else:
        bound_model = model

    # Prepend system prompt if provided
    call_messages = list(messages)
    if system_prompt:
        call_messages = [SystemMessage(content=system_prompt)] + call_messages

    total_tool_calls = 0

    def emit_tool_event(event: str, payload: dict[str, Any]) -> None:
        if not on_tool_event:
            return
        try:
            on_tool_event(event, payload)
        except Exception as e:
            log.debug("Tool event callback failed: %s", e)

    for turn in range(max_turns):
        # Call LLM
        try:
            response: AIMessage = await bound_model.ainvoke(call_messages)
        except Exception as e:
            log.error("LLM call failed (turn %d): %s", turn, e)
            error_msg = AIMessage(content="Произошла ошибка при обработке. Попробуй ещё раз.")
            messages.append(error_msg)
            return AgentResult(
                messages=messages,
                content=error_msg.content,
                tool_calls_count=total_tool_calls,
            )

        messages.append(response)
        call_messages.append(response)

        # No tool calls → done
        if not getattr(response, "tool_calls", None):
            content = response.content if isinstance(response.content, str) else str(response.content)
            return AgentResult(
                messages=messages,
                content=content,
                tool_calls_count=total_tool_calls,
            )

        # Execute tool calls
        tool_messages = []
        for tc in response.tool_calls:
            total_tool_calls += 1
            tool_name = tc.get("name", "")
            tool_call_id = tc.get("id", "")
            tool = tool_map.get(tool_name)
            emit_tool_event("tool_call", {
                "name": tool_name,
                "call_id": tool_call_id,
                "args": tc.get("args", {}),
                "turn": turn + 1,
            })
            tool_started = time.perf_counter()

            if tool is None:
                log.warning("Unknown tool called: '%s'", tool_name)
                tm = ToolMessage(
                    content=f"[ERROR] Unknown tool: '{tool_name}'. Available: {list(tool_map.keys())}",
                    tool_call_id=tool_call_id,
                )
                emit_tool_event("tool_result", {
                    "name": tool_name,
                    "call_id": tool_call_id,
                    "ok": False,
                    "content": tm.content,
                    "turn": turn + 1,
                    "duration_ms": round((time.perf_counter() - tool_started) * 1000),
                })
            else:
                log.info("Executing tool: %s (args: %s)", tool_name, str(tc.get("args", {}))[:200])
                tm = await execute_tool(tool, tc, error_handler)
                log.info("Tool result: %s → %s", tool_name, str(tm.content)[:200])
                emit_tool_event("tool_result", {
                    "name": tool_name,
                    "call_id": tool_call_id,
                    "ok": not str(tm.content).startswith("[ERROR]"),
                    "content": tm.content,
                    "turn": turn + 1,
                    "duration_ms": round((time.perf_counter() - tool_started) * 1000),
                })

            tool_messages.append(tm)

        messages.extend(tool_messages)
        call_messages.extend(tool_messages)

    # Max turns exhausted
    log.warning("React loop exhausted after %d turns", max_turns)
    final = AIMessage(content="Достигнут лимит итераций. Вот что удалось сделать на текущий момент.")
    messages.append(final)
    return AgentResult(
        messages=messages,
        content=final.content,
        tool_calls_count=total_tool_calls,
    )


def create_agent(
    model: BaseChatModel,
    tools: list[BaseTool],
    system_prompt: str,
    name: str = "agent",
    max_turns: int = MAX_REACT_TURNS,
    error_handler: Callable[[Exception], str] = classify_tool_error,
    on_tool_event: ToolEventCallback | None = None,
):
    """Create a reusable agent function (replaces create_react_agent).

    Returns an async callable that takes messages and returns AgentResult.

    Usage:
        agent = create_agent(model, tools, "You are a research agent...")
        result = await agent([HumanMessage(content="search for X")])
    """

    async def run(
        messages: list[BaseMessage],
        extra_tools: list[BaseTool] | None = None,
    ) -> AgentResult:
        all_tools = list(tools)
        if extra_tools:
            all_tools.extend(extra_tools)

        return await react_loop(
            model=model,
            messages=list(messages),  # copy to avoid mutating caller's list
            tools=all_tools,
            system_prompt=system_prompt,
            max_turns=max_turns,
            error_handler=error_handler,
            on_tool_event=on_tool_event,
        )

    run.__name__ = name
    run.__qualname__ = name
    log.info("Agent '%s' created with %d tools", name, len(tools))
    return run
