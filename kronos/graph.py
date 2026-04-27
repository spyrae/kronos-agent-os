"""Main Kronos Agent OS agent pipeline.

Flow: validate → retrieve_memories → route (supervisor or direct) → store_memories → [compact]

No LangGraph — plain async pipeline using kronos.engine.

Memory integration (Mem0):
- retrieve_memories: searches relevant memories before LLM call
- store_memories: saves conversation facts in background after response
- compact: summarizes long conversations when threshold is exceeded
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from kronos.config import settings
from kronos.audit import log_tool_event, reset_tool_audit_context, set_tool_audit_context
from kronos.engine import react_loop
from kronos.llm import get_model
from kronos.memory.context_engine import get_context_engine
from kronos.memory.nodes import retrieve_memories, store_memories_background
from kronos.persona import build_system_prompt
from kronos.router import classify_tier
from kronos.security.shield import validate_input
from kronos.session import SessionStore
from kronos.skills.store import SkillStore
from kronos.skills.tools import (
    approve_skill,
    import_skill_from_source,
    init_skill_tools,
    load_skill,
    load_skill_reference,
)
from kronos.state import AgentState

log = logging.getLogger("kronos.graph")


class KronosAgent:
    """Main agent — wires validation, memory, routing, and persistence.

    Replaces LangGraph's StateGraph + checkpointer.
    """

    def __init__(
        self,
        tools: list[BaseTool] | None = None,
        enable_memory: bool = True,
        enable_supervisor: bool = True,
        session_store: SessionStore | None = None,
        tool_event_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        self._tools: list[BaseTool] = list(tools or [])
        self._enable_memory = enable_memory
        self._memory_enabled = False
        self._supervisor = None
        self._system_prompt: str | None = None
        self._session_store = session_store
        self._external_tool_event_callback = tool_event_callback

        self._init_tools()
        self._init_memory(enable_memory)
        self._init_supervisor(enable_supervisor)

    @property
    def tool_count(self) -> int:
        return len(self._tools)

    def _init_tools(self) -> None:
        """Initialize skill tools, browser tools, gateway tools, etc."""
        # Initialize skill store and add skill tools
        self._skill_store = SkillStore(settings.workspace_path)
        init_skill_tools(self._skill_store)

        if self._skill_store.list_skills():
            self._tools.extend([load_skill, load_skill_reference, approve_skill, import_skill_from_source])
            log.info("Skill tools added: %d skills", len(self._skill_store.list_skills()))

        # Browser tools
        from kronos.tools.browser.tools import get_browser_tools
        browser_tools = get_browser_tools()
        if browser_tools:
            self._tools.extend(browser_tools)
            log.info("Browser tools added: %d", len(browser_tools))

        # MCP gateway tools. Mutating runtime server management is opt-in.
        from kronos.tools.gateway_tools import get_gateway_tools
        self._tools.extend(get_gateway_tools())

        # Dynamic tools are powerful but risky; keep disabled in public-safe
        # defaults unless a trusted local deployment explicitly enables them.
        if settings.enable_dynamic_tools:
            from kronos.tools.dynamic import load_persisted_tools
            from kronos.tools.dynamic_tools import get_dynamic_management_tools
            self._tools.extend(get_dynamic_management_tools())
            persisted = load_persisted_tools()
            if persisted:
                self._tools.extend(persisted)
                log.info("Dynamic tools: %d persisted", len(persisted))
        else:
            log.info("Dynamic tools disabled (ENABLE_DYNAMIC_TOOLS=false)")

        # Session search tool
        from kronos.tools.session_search import session_search
        self._tools.append(session_search)

        # Composio tools
        from kronos.tools.composio_integration import get_composio_tools
        composio_tools = get_composio_tools()
        if composio_tools:
            self._tools.extend(composio_tools)
            log.info("Composio tools added: %d", len(composio_tools))

        # Context engine
        get_context_engine(settings.context_strategy)

    def _init_memory(self, enable: bool) -> None:
        """Initialize memory (requires DeepSeek for fact extraction)."""
        self._memory_enabled = enable and bool(settings.deepseek_api_key)
        if enable and not self._memory_enabled:
            log.info("Memory disabled: DEEPSEEK_API_KEY not set")

    def _init_supervisor(self, enable: bool) -> None:
        """Build supervisor if tools are available."""
        if enable and self._tools:
            from kronos.agents.supervisor import build_supervisor
            self._supervisor = build_supervisor(self._tools, on_tool_event=self._emit_tool_event)
            if self._supervisor:
                log.info("Multi-agent supervisor enabled")

    def _emit_tool_event(self, event: str, payload: dict[str, Any]) -> None:
        """Persist tool events and fan out to optional runtime callbacks."""
        log_tool_event(event, payload)
        if self._external_tool_event_callback:
            self._external_tool_event_callback(event, payload)

    def _get_system_prompt(self) -> str:
        """Build system prompt (cached)."""
        if self._system_prompt is None:
            catalog = self._skill_store.build_catalog() if self._skill_store else ""
            self._system_prompt = build_system_prompt(
                settings.workspace_path, skill_catalog=catalog,
            )
            log.info("System prompt: %d chars, skills: %d chars", len(self._system_prompt), len(catalog))
        return self._system_prompt

    async def ainvoke(
        self,
        message: str,
        thread_id: str,
        user_id: str = "",
        session_id: str = "",
        source_kind: str = "user",
        persist_user_turn: bool = True,
        extra_system_context: str = "",
    ) -> str:
        """Process a message and return the response text.

        Args:
            message: raw user text (no bridge wrappers). For peer reactions
                (source_kind="peer_reaction") this may be a short instruction
                such as the peer's answer framed as a prompt — but it must
                NOT contain transport metadata, agent tags, or peer wrappers.
            thread_id: conversation thread key (chat_id[:topic_id]).
            user_id / session_id: audit + memory scoping.
            source_kind: "user" for normal user messages, "peer_reaction"
                when the agent is reacting to another bot's reply. Peer
                reactions are ephemeral — they never mutate persisted
                history or long-term memory.
            persist_user_turn: if False, the incoming `message` and the
                resulting response are NOT written back to session_store.
                Used for peer reactions so transient peer context never
                pollutes the agent's history and causes parroting.
            extra_system_context: transient SystemMessage prepended for
                THIS invocation only (never persisted). Use this to carry
                group-chat metadata ("this is a group chat, reply as
                yourself, be concise") or the text of the peer answer the
                agent is reacting to.

        Handles the full pipeline: load history → validate → memory →
        route → store memory → compact → save history.
        """
        is_ephemeral = not persist_user_turn

        # Load conversation history
        history: list[BaseMessage] = []
        if self._session_store:
            history = await self._session_store.load(thread_id)

        # Transient system context for this call only (never persisted).
        transient_prefix: list[BaseMessage] = []
        if extra_system_context:
            transient_prefix.append(SystemMessage(content=extra_system_context))

        # Working history = persistent history + transient prefix + new user turn.
        # `persisted_history` tracks what will be saved if persist_user_turn=True.
        persisted_history = list(history)
        persisted_history.append(HumanMessage(content=message))
        working_history: list[BaseMessage] = transient_prefix + persisted_history

        # Build state for memory/validation nodes (always sees working history).
        state: AgentState = {
            "messages": working_history,
            "user_id": user_id,
            "session_id": session_id,
            "safety_passed": True,
            "loop_detector": None,
        }

        # Step 1: Validate input
        rejection = validate_input(message, source=user_id)
        if rejection:
            log.warning("Input rejected for user %s", user_id)
            response_text = rejection
            if not is_ephemeral:
                persisted_history.append(AIMessage(content=response_text))
                if self._session_store:
                    await self._session_store.save(thread_id, persisted_history)
            return response_text

        # Step 2: Retrieve memories (non-fatal — DB issues must not crash pipeline)
        if self._memory_enabled:
            try:
                mem_update = retrieve_memories(state)
                if mem_update.get("messages"):
                    # Insert memory context before the last user message in
                    # working history only — never into persisted_history.
                    insert_at = len(working_history) - 1
                    for mem_msg in mem_update["messages"]:
                        working_history.insert(insert_at, mem_msg)
                        insert_at += 1
            except Exception as e:
                log.warning("Memory retrieval failed (non-fatal): %s", e)

        # Step 3: Route — supervisor or direct LLM
        audit_token = set_tool_audit_context(
            agent=settings.agent_name,
            thread_id=thread_id,
            user_id=user_id,
            session_id=session_id,
            source_kind=source_kind,
        )
        try:
            if self._supervisor:
                result = await self._supervisor(working_history)
                response_text = result.content
            else:
                # Direct LLM call with tools (single-agent mode)
                tier = classify_tier(message)
                model = get_model(tier)
                result = await react_loop(
                    model=model,
                    messages=working_history,
                    tools=self._tools,
                    system_prompt=self._get_system_prompt(),
                    on_tool_event=self._emit_tool_event,
                )
                response_text = result.content
        finally:
            reset_tool_audit_context(audit_token)

        if not is_ephemeral:
            persisted_history.append(AIMessage(content=response_text))

        # Step 4: Store memories — skip entirely for ephemeral peer reactions.
        if self._memory_enabled and not is_ephemeral:
            mem_state = {**state, "messages": list(persisted_history)}
            asyncio.get_event_loop().run_in_executor(
                None, store_memories_background, mem_state,
            )

        # Step 5: Compact if needed (only for real user turns).
        if self._memory_enabled and not is_ephemeral:
            engine = get_context_engine()
            compact_state = {**state, "messages": list(persisted_history)}
            if engine.should_compact(compact_state):
                compact_result = engine.compact(compact_state)
                if compact_result.get("messages"):
                    persisted_history = compact_result["messages"]

        # Step 6: Save conversation history.
        if self._session_store and not is_ephemeral:
            save_messages = [m for m in persisted_history if not isinstance(m, SystemMessage)]
            await self._session_store.save(thread_id, save_messages)

        return response_text

    async def clear_context(self, thread_id: str) -> str:
        """Clear conversation history for a thread."""
        if self._session_store:
            deleted = await self._session_store.clear(thread_id)
            log.info("Cleared context for thread %s: %d rows", thread_id, deleted)
            return f"🧹 Контекст очищен (thread: {thread_id}, удалено: {deleted})"
        return "Session store не настроен."
