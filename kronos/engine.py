"""Custom agent engine — replaces LangGraph's create_react_agent + ToolNode.

Provides:
- execute_tool(): run a single tool with error handling
- react_loop(): LLM ↔ tool execution loop (the core ReAct pattern)
- create_agent(): factory that returns a reusable agent function

No LangGraph dependency. Uses langchain_core messages and tools directly.
"""

import asyncio
import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool

from kronos.config import settings
from kronos.security.loop_detector import LoopDetector, LoopLevel, get_nudge_message
from kronos.security.sanitize import wrap_untrusted
from kronos.tools.error_handler import classify_tool_error

log = logging.getLogger("kronos.engine")

MAX_REACT_TURNS = 25
TOOL_TIMEOUT_SECONDS = 120
ToolEventCallback = Callable[[str, dict[str, Any]], None]
MessageDeltaCallback = Callable[[list[BaseMessage]], Any]
ToolCacheGetCallback = Callable[[str], Any]
ToolCacheSaveCallback = Callable[[str, str], Any]
ToolApprovalPredicate = Callable[[BaseTool, dict], Any]
ToolApprovalRequestCallback = Callable[[BaseTool, dict], Any]

RAW_TOOL_CONTENT_KEY = "raw_content"
MODEL_OUTPUT_MAX_CHARS = 2400
MODEL_OUTPUT_MAX_ITEMS = 8

DEFAULT_APPROVAL_TOOL_NAMES = {
    "mcp_add_server",
    "mcp_remove_server",
    "mcp_reload",
    "create_new_tool",
    "approve_skill",
    "import_skill_from_source",
    "add_expense",
    "add_tranche",
    "replace_tranche",
    "resolve_pending_expense",
}
READ_ONLY_TOOL_PREFIXES = (
    "get_",
    "list_",
    "read_",
    "search_",
    "fetch_",
    "inspect_",
    "describe_",
)
DEFAULT_APPROVAL_ACTION_PREFIXES = (
    "deploy",
    "restart",
    "send",
    "post",
    "publish",
    "delete",
    "remove",
    "write",
    "update",
)
DEFAULT_APPROVAL_NAME_MARKERS = (
    "deploy",
    "restart",
    "send_",
    "post_",
    "publish",
    "delete",
    "remove",
    "write",
    "update",
)

# Whether risky tool calls pause for human approval is a config gate
# (settings.tool_approvals_enabled, ON by default). Read at call time so env
# overrides and tests take effect without reimporting the module.

DEFAULT_COMPACT_OUTPUT_NAME_MARKERS = (
    "brave",
    "exa",
    "web_search",
    "search",
    "fetch",
    "content",
    "extract",
    "reddit",
    "transcript",
    "tg_channel",
    "tg_channels",
    "channel",
    "digest",
    "compare",
    "dump",
    "query",
    "logs",
)


@dataclass
class AgentResult:
    """Result of an agent execution."""

    messages: list[BaseMessage]
    content: str  # final text response
    tool_calls_count: int = 0
    waiting_approval: bool = False
    approval_id: str | None = None
    approval_tool_name: str | None = None


def tool_requires_approval(tool: BaseTool, args: dict) -> bool:
    """Return whether a tool call should pause for human approval."""
    if not settings.tool_approvals_enabled:
        return False

    metadata = getattr(tool, "metadata", None) or {}
    declared = metadata.get("needs_approval")
    if callable(declared):
        return bool(declared(args))
    if declared is not None:
        return bool(declared)

    declared_attr = getattr(tool, "needs_approval", None)
    if callable(declared_attr):
        return bool(declared_attr(args))
    if declared_attr is not None:
        return bool(declared_attr)

    name = tool.name.lower()
    if name in DEFAULT_APPROVAL_TOOL_NAMES:
        return True
    if name.startswith(READ_ONLY_TOOL_PREFIXES):
        return False
    if name.startswith(DEFAULT_APPROVAL_ACTION_PREFIXES):
        return True
    return any(marker in name for marker in DEFAULT_APPROVAL_NAME_MARKERS)


def _render_tool_result(value: Any) -> str:
    """Render a tool result to the legacy string representation."""
    return str(value) if value is not None else "OK"


def _jsonable(value: Any) -> Any:
    """Best-effort conversion for compact model-facing summaries."""
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:
            return value
    return value


def _clip(text: str, limit: int) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)].rstrip() + "..."


def _compact_item(item: Any, index: int) -> str:
    data = _jsonable(item)
    if isinstance(data, dict):
        title = data.get("title") or data.get("name") or data.get("url") or f"item {index}"
        url = data.get("url") or data.get("link") or data.get("href") or ""
        description = (
            data.get("description")
            or data.get("summary")
            or data.get("text")
            or data.get("content")
            or data.get("snippet")
            or ""
        )
        suffix = f" — {url}" if url else ""
        body = f": {_clip(str(description), 220)}" if description else ""
        return f"{index}. {_clip(str(title), 140)}{suffix}{body}"
    return f"{index}. {_clip(str(data), 260)}"


def compact_tool_output(result: Any) -> str:
    """Return a token-light representation of large tool results."""
    if isinstance(result, (list, tuple)):
        items = [_compact_item(item, idx) for idx, item in enumerate(result[:MODEL_OUTPUT_MAX_ITEMS], start=1)]
        hidden = len(result) - len(items)
        suffix = f"\n... {hidden} more item(s) omitted." if hidden > 0 else ""
        return "Tool result summary for model:\n" + "\n".join(items) + suffix

    raw = _render_tool_result(result)
    if len(raw) <= MODEL_OUTPUT_MAX_CHARS:
        return raw
    return (
        f"[COMPRESSED tool output: {len(raw)} chars; full output is available "
        "in tool_result event/audit.]\n"
        f"{raw[:MODEL_OUTPUT_MAX_CHARS].rstrip()}..."
    )


def _default_should_compact_tool_output(tool: BaseTool) -> bool:
    name = tool.name.lower()
    return any(marker in name for marker in DEFAULT_COMPACT_OUTPUT_NAME_MARKERS)


def _tool_output_is_untrusted(tool: BaseTool) -> bool:
    """Whether a tool returns attacker-controllable external content.

    Such output (web pages, fetched documents, third-party API bodies) must be
    handed to the model as DATA, not trusted text, so an instruction injected
    into it is not obeyed. Opt in per tool via ``metadata['untrusted_output']``
    or an ``untrusted_output`` attribute — the same pattern as ``needs_approval``.
    """
    metadata = getattr(tool, "metadata", None) or {}
    flag = metadata.get("untrusted_output")
    if flag is None:
        flag = getattr(tool, "untrusted_output", None)
    return bool(flag)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _tool_model_output(tool: BaseTool, result: Any) -> tuple[str, str]:
    """Return (model_content, raw_content) for a successful tool result."""
    raw_content = _render_tool_result(result)
    metadata = getattr(tool, "metadata", None) or {}
    converter = metadata.get("to_model_output") or getattr(tool, "to_model_output", None)

    if callable(converter):
        try:
            model_content = await _maybe_await(converter(result))
            return _render_tool_result(model_content), raw_content
        except Exception as e:
            log.warning("Tool to_model_output failed for %s: %s", tool.name, e)

    if _default_should_compact_tool_output(tool):
        return compact_tool_output(result), raw_content
    return raw_content, raw_content


def tool_message_raw_content(message: ToolMessage) -> str:
    """Return full raw tool content when a ToolMessage stores compressed text."""
    return str(message.additional_kwargs.get(RAW_TOOL_CONTENT_KEY, message.content))


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

        content, raw_content = await _tool_model_output(tool, result)
        if _tool_output_is_untrusted(tool):
            # Attacker-controllable content — frame it as data so an injected
            # instruction inside it is not executed. raw_content (kept for the
            # audit journal) stays the unwrapped original.
            content = wrap_untrusted(content, label=f"tool:{tool.name}")

    except TimeoutError:
        content = f"[ERROR] Tool '{tool.name}' timed out after {TOOL_TIMEOUT_SECONDS}s"
        raw_content = content
        log.error("Tool timeout: %s", tool.name)
    except Exception as e:
        content = error_handler(e)
        raw_content = content
        log.warning("Tool error %s: %s", tool.name, str(e)[:200])

    additional_kwargs = {}
    if raw_content != content:
        additional_kwargs[RAW_TOOL_CONTENT_KEY] = raw_content
    return ToolMessage(
        content=content,
        tool_call_id=tool_call_id,
        additional_kwargs=additional_kwargs,
    )


def _deferred_tool_messages(tool_calls: list[dict], awaiting: dict) -> list["ToolMessage"]:
    """Placeholder results for tool_calls that follow the one now awaiting approval.

    When the loop returns mid-batch for approval, every OTHER tool_call in the
    same AIMessage still needs a ToolMessage — otherwise the resumed history
    has an assistant turn whose tool_calls aren't all answered, which is a hard
    protocol error on the next LLM call. The awaiting call is filled in on
    resume; the ones after it get these deferred placeholders now, and the
    model re-issues them if it still needs the results.
    """
    awaiting_id = awaiting.get("id", "")
    deferred: list[ToolMessage] = []
    seen_awaiting = False
    for tc in tool_calls:
        if tc.get("id", "") == awaiting_id:
            seen_awaiting = True
            continue
        if seen_awaiting:
            deferred.append(
                ToolMessage(
                    content=(
                        "[deferred] A prior tool call in this batch is awaiting "
                        "approval, so this call was not executed. Re-issue it if "
                        "you still need the result."
                    ),
                    tool_call_id=tc.get("id", ""),
                )
            )
    return deferred


async def react_loop(
    model: BaseChatModel,
    messages: list[BaseMessage],
    tools: list[BaseTool],
    system_prompt: str | None = None,
    max_turns: int = MAX_REACT_TURNS,
    error_handler: Callable[[Exception], str] = classify_tool_error,
    on_tool_event: ToolEventCallback | None = None,
    on_message_delta: MessageDeltaCallback | None = None,
    get_cached_tool_result: ToolCacheGetCallback | None = None,
    save_tool_result: ToolCacheSaveCallback | None = None,
    needs_tool_approval: ToolApprovalPredicate | None = None,
    request_tool_approval: ToolApprovalRequestCallback | None = None,
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

    async def maybe_await(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def emit_message_delta(delta: list[BaseMessage]) -> None:
        if not on_message_delta or not delta:
            return
        try:
            await maybe_await(on_message_delta(delta))
        except Exception as e:
            log.warning("Message journal callback failed (non-fatal): %s", e)

    async def read_cached_tool_result(tool_call_id: str) -> str | None:
        if not get_cached_tool_result or not tool_call_id:
            return None
        try:
            cached = await maybe_await(get_cached_tool_result(tool_call_id))
            return str(cached) if cached is not None else None
        except Exception as e:
            log.warning("Tool result cache read failed (non-fatal): %s", e)
            return None

    async def write_tool_result(tool_call_id: str, content: str) -> None:
        if not save_tool_result or not tool_call_id:
            return
        try:
            await maybe_await(save_tool_result(tool_call_id, content))
        except Exception as e:
            log.warning("Tool result cache write failed (non-fatal): %s", e)

    async def requires_approval(tool: BaseTool, tool_call: dict) -> bool:
        if not settings.tool_approvals_enabled:
            return False

        predicate = needs_tool_approval or tool_requires_approval
        try:
            return bool(await maybe_await(predicate(tool, tool_call.get("args", {}) or {})))
        except Exception as e:
            log.warning("Tool approval predicate failed for %s: %s", tool.name, e)
            return True

    async def create_pending_approval(tool: BaseTool, tool_call: dict) -> str | None:
        if not request_tool_approval:
            return None
        try:
            approval_id = await maybe_await(request_tool_approval(tool, tool_call))
            return str(approval_id) if approval_id else None
        except Exception as e:
            log.warning("Tool approval request failed for %s: %s", tool.name, e)
            return None

    # Detect runaway tool loops across turns (same call repeated, ping-pong,
    # polling with no progress). Nudges the model to change course, and aborts
    # via circuit breaker if it stays stuck — a cost/safety backstop.
    loop_detector = LoopDetector()
    last_nudge_level = LoopLevel.OK

    for turn in range(max_turns):
        # Call LLM
        try:
            response: AIMessage = await bound_model.ainvoke(call_messages)
        except Exception as e:
            log.error("LLM call failed (turn %d): %s", turn, e)
            error_msg = AIMessage(content="Произошла ошибка при обработке. Попробуй ещё раз.")
            messages.append(error_msg)
            await emit_message_delta([error_msg])
            return AgentResult(
                messages=messages,
                content=error_msg.content,
                tool_calls_count=total_tool_calls,
            )

        messages.append(response)
        call_messages.append(response)
        await emit_message_delta([response])

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
            emit_tool_event(
                "tool_call",
                {
                    "name": tool_name,
                    "call_id": tool_call_id,
                    "args": tc.get("args", {}),
                    "turn": turn + 1,
                },
            )
            tool_started = time.perf_counter()

            if tool is None:
                log.warning("Unknown tool called: '%s'", tool_name)
                tm = ToolMessage(
                    content=f"[ERROR] Unknown tool: '{tool_name}'. Available: {list(tool_map.keys())}",
                    tool_call_id=tool_call_id,
                )
                await write_tool_result(tool_call_id, str(tm.content))
                raw_content = tool_message_raw_content(tm)
                model_content = str(tm.content)
                emit_tool_event(
                    "tool_result",
                    {
                        "name": tool_name,
                        "call_id": tool_call_id,
                        "ok": False,
                        "content": raw_content,
                        "model_content": model_content,
                        "compressed": raw_content != model_content,
                        "raw_content_chars": len(raw_content),
                        "model_content_chars": len(model_content),
                        "turn": turn + 1,
                        "duration_ms": round((time.perf_counter() - tool_started) * 1000),
                    },
                )
            else:
                cached_content = await read_cached_tool_result(tool_call_id)
                if cached_content is not None:
                    tm = ToolMessage(content=cached_content, tool_call_id=tool_call_id)
                    log.info("Tool result cache hit: %s (%s)", tool_name, tool_call_id)
                else:
                    if await requires_approval(tool, tc):
                        approval_id = await create_pending_approval(tool, tc)
                        if approval_id:
                            if tool_messages:
                                messages.extend(tool_messages)
                                call_messages.extend(tool_messages)
                                await emit_message_delta(tool_messages)
                            # Tool calls after this one in the same response are
                            # not executed in this pass. Emit deferred results
                            # for them so the resumed history has a ToolMessage
                            # for every tool_call except the one being approved
                            # (which is filled in on resume) — otherwise the
                            # next LLM call fails with an unanswered-tool_call
                            # protocol error.
                            deferred = _deferred_tool_messages(response.tool_calls, tc)
                            if deferred:
                                messages.extend(deferred)
                                call_messages.extend(deferred)
                                await emit_message_delta(deferred)
                            content = (
                                "⚠️ Нужно подтверждение перед выполнением tool-call.\n"
                                f"Tool: `{tool_name}`\n"
                                f"Approval ID: `{approval_id}`\n\n"
                                "Нажми Approve/Reject в Telegram или обработай approval вручную."
                            )
                            emit_tool_event(
                                "tool_approval_required",
                                {
                                    "name": tool_name,
                                    "call_id": tool_call_id,
                                    "approval_id": approval_id,
                                    "turn": turn + 1,
                                },
                            )
                            return AgentResult(
                                messages=messages,
                                content=content,
                                tool_calls_count=total_tool_calls,
                                waiting_approval=True,
                                approval_id=approval_id,
                                approval_tool_name=tool_name,
                            )

                        content = f"[ERROR] Tool '{tool_name}' requires approval, but no approval handler is available."
                        tm = ToolMessage(content=content, tool_call_id=tool_call_id)
                        await write_tool_result(tool_call_id, str(tm.content))
                    else:
                        log.info("Executing tool: %s (args: %s)", tool_name, str(tc.get("args", {}))[:200])
                        tm = await execute_tool(tool, tc, error_handler)
                        await write_tool_result(tool_call_id, str(tm.content))
                        log.info("Tool result: %s → %s", tool_name, str(tm.content)[:200])
                raw_content = tool_message_raw_content(tm)
                model_content = str(tm.content)
                emit_tool_event(
                    "tool_result",
                    {
                        "name": tool_name,
                        "call_id": tool_call_id,
                        "ok": not raw_content.startswith("[ERROR]"),
                        "content": raw_content,
                        "model_content": model_content,
                        "compressed": raw_content != model_content,
                        "raw_content_chars": len(raw_content),
                        "model_content_chars": len(model_content),
                        "cached": cached_content is not None,
                        "turn": turn + 1,
                        "duration_ms": round((time.perf_counter() - tool_started) * 1000),
                    },
                )

            tool_messages.append(tm)
            loop_detector.record(tool_name, tc.get("args", {}) or {}, tool_message_raw_content(tm))

        messages.extend(tool_messages)
        call_messages.extend(tool_messages)
        await emit_message_delta(tool_messages)

        # Loop backstop: nudge on WARNING/CRITICAL (once per escalation) so the
        # next turn changes course; abort on CIRCUIT_BREAKER with a partial
        # result instead of burning turns/budget on a stuck loop.
        level, desc = loop_detector.check()
        if level == LoopLevel.CIRCUIT_BREAKER:
            final = AIMessage(content=get_nudge_message(level, desc))
            messages.append(final)
            await emit_message_delta([final])
            log.warning("React loop circuit breaker: %s", desc)
            return AgentResult(
                messages=messages,
                content=final.content,
                tool_calls_count=total_tool_calls,
            )
        if level in (LoopLevel.WARNING, LoopLevel.CRITICAL) and level != last_nudge_level:
            nudge = SystemMessage(content=get_nudge_message(level, desc))
            messages.append(nudge)
            call_messages.append(nudge)
            await emit_message_delta([nudge])
            last_nudge_level = level
            log.info("React loop nudge (%s): %s", level, desc)

    # Max turns exhausted
    log.warning("React loop exhausted after %d turns", max_turns)
    final = AIMessage(content="Достигнут лимит итераций. Вот что удалось сделать на текущий момент.")
    messages.append(final)
    await emit_message_delta([final])
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
