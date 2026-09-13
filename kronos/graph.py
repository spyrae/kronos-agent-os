"""Main Kronos Agent OS agent pipeline.

Flow: validate → retrieve_memories → route (supervisor or direct) → store_memories → [compact]

No LangGraph — plain async pipeline using kronos.engine.

Memory integration (Mem0):
- retrieve_memories: searches relevant memories before LLM call
- store_memories: saves conversation facts in background after response
- compact: summarizes long conversations when threshold is exceeded
"""

import asyncio
import json
import logging
from collections.abc import Callable
from contextvars import copy_context
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool

from kronos.audit import log_tool_event, reset_tool_audit_context, set_tool_audit_context
from kronos.config import settings
from kronos.effect_state import DurableStateError
from kronos.engine import (
    AgentResult,
    SubAgentApprovalPause,
    ToolEventCallback,
    clear_delegation_ctx,
    current_delegation,
    execute_tool,
    publish_delegation_ctx,
    react_loop,
    tool_requires_approval,
)
from kronos.execution_control import ExecutionStoppedError, check_execution, execution_scope, run_sync_owned
from kronos.llm import get_model
from kronos.memory.context_engine import get_context_engine
from kronos.memory.nodes import retrieve_memories, store_memories_background
from kronos.outcomes import InvocationOutcome
from kronos.persona import build_system_prompt
from kronos.router import classify_tier
from kronos.security.model_budget import model_budget_scope
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
from kronos.turn_delivery import RecoveryDestination
from kronos.turn_ownership import TurnBusyError, TurnOwnership, own_conversation

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
        self._durable_recovery_checked = False
        self._last_pending_approval_id: str | None = None

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

        # Reading the web: one-shot fetch + field extraction. Always available —
        # they need no browser install and degrade to a plain fetch.
        from kronos.tools.acquire import ACQUIRE_TOOLS

        self._tools.extend(ACQUIRE_TOOLS)

        # Adding money up is where invented numbers come from, so it is a
        # function rather than something the model does in its head.
        from kronos.tools.compare import compare_offers

        self._tools.append(compare_offers)

        # Running a short program beats doing arithmetic in prose. Opt-in, and
        # only ever inside the container.
        if settings.enable_code_execution:
            from kronos.tools.code import CODE_TOOLS

            self._tools.extend(CODE_TOOLS)
            log.info("Code execution tool added (sandboxed)")

        # Working as the owner on configured sites. The tools hand out sessions,
        # never secrets.
        from kronos.tools.accounts_tools import ACCOUNT_TOOLS

        self._tools.extend(ACCOUNT_TOOLS)

        # Work that will not finish in this conversation: a goal, steps, and what
        # each step waits for. The cron poller is what actually runs them.
        from kronos.tools.plans_tools import PLAN_TOOLS

        self._tools.extend(PLAN_TOOLS)

        # Reading registered repositories. Added only when there are any, so an
        # agent with no repositories is not offered six tools that all say no.
        from kronos.repos import list_repos
        from kronos.tools.repo_tools import REPO_TOOLS

        try:
            if list_repos():
                self._tools.extend(REPO_TOOLS)
                log.info("Repository tools added")
        except Exception as e:
            log.warning("Repository registry unavailable: %s", e)

        # Browser tools (stateful session; the acquire tools cover one-off reads)
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

        # Scheduled tasks / reminders (roadmap 4.2)
        from kronos.tools.reminders import (
            cancel_scheduled_task,
            list_scheduled_tasks,
            schedule_followup,
            schedule_task,
        )

        self._tools.extend([schedule_task, schedule_followup, list_scheduled_tasks, cancel_scheduled_task])

        # Swarm collaboration: hand-off (5.1) + council (5.2) + memory query (5.3)
        from kronos.tools.council import convene_council
        from kronos.tools.handoff import handoff_to_agent
        from kronos.tools.memory_ask import ask_agent_memory

        self._tools.extend([handoff_to_agent, convene_council, ask_agent_memory])

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
                settings.workspace_path,
                skill_catalog=catalog,
            )
            log.info("System prompt: %d chars, skills: %d chars", len(self._system_prompt), len(catalog))
        return self._system_prompt

    @property
    def session_store(self) -> SessionStore | None:
        """The runtime's durable store, shared with caller-specific recovery."""
        return self._session_store

    @property
    def last_pending_approval_id(self) -> str | None:
        """Most recent approval id returned to the transport layer."""
        return self._last_pending_approval_id

    def _approval_tool_map(self) -> dict[str, BaseTool]:
        """Return tools eligible for approval resume by name."""
        tool_map = {tool.name: tool for tool in self._tools}
        if self._supervisor:
            for tool in getattr(self._supervisor, "_approval_tools", []):
                tool_map[tool.name] = tool
        return tool_map

    async def get_pending_tool_approval(self, approval_id: str) -> dict[str, Any] | None:
        """Return a pending tool approval without claiming it."""
        if not self._session_store:
            return None
        return await self._session_store.get_pending_approval(approval_id)

    def _build_durable_react_loop_kwargs(
        self,
        *,
        turn_id: str,
        thread_id: str,
        approved_tool_name: str | None = None,
        approved_tool_args: dict | None = None,
    ) -> dict[str, Any]:
        """Build journal/cache/approval callbacks for a durable turn."""
        if not self._session_store:
            return {}

        async def journal_delta(delta: list[BaseMessage]) -> None:
            await self._session_store.append_turn_messages(
                turn_id=turn_id,
                thread_id=thread_id,
                messages=delta,
            )

        async def get_cached_tool_result(tool_call_id: str) -> str | None:
            return await self._session_store.get_tool_result(turn_id, tool_call_id)

        async def save_tool_result(tool_call_id: str, content: str) -> None:
            await self._session_store.save_tool_result(
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                content=content,
            )

        async def get_external_effect(key: str) -> str | None:
            return await self._session_store.get_external_effect(key)

        async def recover_tool_result(tool_call: dict) -> str | None:
            return await self._session_store.get_recorded_call_effect(turn_id, tool_call)

        async def record_external_effect(key: str, tool_name: str, result: str) -> None:
            await self._session_store.record_external_effect(
                key=key,
                turn_id=turn_id,
                tool=tool_name,
                result=result,
            )

        async def begin_external_effect(key: str, tool_name: str, args: dict, tool_call_id: str, dedupe_by_key: bool):
            return await self._session_store.begin_external_effect(
                key=key,
                turn_id=turn_id,
                tool=tool_name,
                args=args,
                tool_call_id=tool_call_id,
                dedupe_by_key=dedupe_by_key,
            )

        async def finish_external_effect(key: str, token: str, tool_name: str, result: str) -> None:
            await self._session_store.finish_external_effect(
                key=key,
                token=token,
                turn_id=turn_id,
                tool=tool_name,
                result=result,
            )

        async def request_tool_approval(tool: BaseTool, tool_call: dict) -> str:
            # current_delegation() is set when the approval originates inside a
            # sub-agent — it records the parent delegate_to_X call so the resume
            # re-runs that delegation with this call exempted.
            return await self._session_store.create_pending_approval(
                turn_id=turn_id,
                thread_id=thread_id,
                tool_call_id=str(tool_call.get("id", "")),
                tool_name=tool.name,
                args=tool_call.get("args", {}) or {},
                delegation=current_delegation(),
            )

        kwargs = {
            "on_message_delta": journal_delta,
            "get_cached_tool_result": get_cached_tool_result,
            "save_tool_result": save_tool_result,
            "request_tool_approval": request_tool_approval,
            "get_external_effect": get_external_effect,
            "recover_tool_result": recover_tool_result,
            "record_external_effect": record_external_effect,
            "begin_external_effect": begin_external_effect,
            "finish_external_effect": finish_external_effect,
            "turn_id": turn_id,
        }
        if approved_tool_name:
            kwargs["needs_tool_approval"] = self._approval_scope(approved_tool_name, approved_tool_args)

        return kwargs

    def _approval_scope(self, approved_tool_name: str, approved_tool_args: dict | None):
        """Predicate exempting exactly the approved (name, args) call.

        The exemption must match the EXACT call the user approved, not just the
        tool name. Binding to the name alone let an approved restart_service("x")
        wave through restart_service("y") during the resume. Compare canonical
        (sorted) args so only the approved call skips a fresh approval; any other
        args (or any other tool) re-prompt.
        """
        approved_args_key = json.dumps(approved_tool_args or {}, sort_keys=True, default=str)

        async def approval_scope(tool: BaseTool, args: dict) -> bool:
            same_call = tool.name == approved_tool_name and (
                json.dumps(args or {}, sort_keys=True, default=str) == approved_args_key
            )
            if same_call:
                return False
            return tool_requires_approval(tool, args)

        return approval_scope

    def _execution_guard(self, thread_id: str, turn_id: str) -> Callable[[], None] | None:
        """Bind plan authority to its durable turn, not a mutable agent field."""
        if not thread_id.startswith("plan:"):
            return None
        from kronos import plans

        agent_name = settings.agent_name
        return lambda: plans.assert_turn_active(thread_id, turn_id, agent_name)

    async def _run_guarded_model_loop(self, *, thread_id: str, turn_id: str | None, **kwargs) -> AgentResult:
        """Keep nested provider/tool calls inside the caller's revocable scope."""
        guard = self._execution_guard(thread_id, turn_id) if turn_id else None
        with execution_scope(guard), model_budget_scope(kwargs.get("force_tier")):
            check_execution()
            result = await self._run_model_loop(**kwargs)
            check_execution()
            return result

    async def _run_model_loop(
        self,
        *,
        messages: list[BaseMessage],
        source_message: str,
        react_loop_kwargs: dict[str, Any],
        on_tool_event: ToolEventCallback | None = None,
        force_tier: str | None = None,
    ) -> AgentResult:
        """Run either the supervisor or direct model loop.

        The agent-level callback handles audit/logging; a per-call callback
        (e.g. live progress in the bridge) is layered on top so both fire.
        force_tier overrides tier classification (cost-guardian degradation).
        """
        emit = self._emit_tool_event
        if on_tool_event is not None:

            def emit(event: str, payload: dict[str, Any], _extra=on_tool_event) -> None:
                self._emit_tool_event(event, payload)
                _extra(event, payload)

        if self._supervisor:
            return await self._supervisor(messages, on_tool_event=emit, **react_loop_kwargs)

        tier = force_tier or classify_tier(source_message)
        model = get_model(tier)
        return await react_loop(
            model=model,
            messages=messages,
            tools=self._tools,
            system_prompt=self._get_system_prompt(),
            on_tool_event=emit,
            **react_loop_kwargs,
        )

    async def resolve_tool_approval(
        self,
        approval_id: str,
        approved: bool,
        decided_by: str = "",
    ) -> str:
        """Resolve approval under the same ownership as normal invocation."""
        if not self._session_store:
            return "Approval state недоступен: session store не настроен."
        pending = await self._session_store.get_pending_approval(approval_id)
        if not pending:
            return "Этот approval уже обработан или не найден."
        async with own_conversation(self._session_store.db_path, str(pending["thread_id"])):
            return await self._resolve_owned_approval(approval_id, approved, decided_by)

    async def _resolve_owned_approval(
        self,
        approval_id: str,
        approved: bool,
        decided_by: str = "",
    ) -> str:
        """Resolve a pending tool approval and resume the durable turn."""
        self._last_pending_approval_id = None
        if not self._session_store:
            return "Approval state недоступен: session store не настроен."

        pending = await self._session_store.get_pending_approval(approval_id)
        if pending and str(pending.get("thread_id", "")).startswith("plan:"):
            from kronos import plans

            plan = plans.plan_for_turn(str(pending["turn_id"]), settings.agent_name)
            if not plan or pending["thread_id"] != f"plan:{plan['id']}":
                return "План остановлен или связь с шагом потеряна. Approval не выполнен."

        pending = await self._session_store.claim_pending_approval(
            approval_id=approval_id,
            decision="approved" if approved else "rejected",
            decided_by=decided_by,
        )
        if not pending:
            return "Этот approval уже обработан или не найден."

        turn_id = str(pending["turn_id"])
        thread_id = str(pending["thread_id"])
        tool_call_id = str(pending["tool_call_id"])
        tool_name = str(pending["tool_name"])
        try:
            messages = await self._session_store.load_turn_messages(thread_id, turn_id)
        except DurableStateError:
            await self._session_store.fail_turn(turn_id, "invalid durable journal; review required")
            raise

        args = pending.get("args", {}) or {}
        delegation = pending.get("delegation")

        # Build loop kwargs up front so a nested (sub-agent) re-run can reuse
        # its approval hooks: the exemption for the approved call plus the
        # pending channel for any further approval it triggers.
        react_loop_kwargs = self._build_durable_react_loop_kwargs(
            turn_id=turn_id,
            thread_id=thread_id,
            approved_tool_name=tool_name if approved else None,
            approved_tool_args=args if approved else None,
        )

        try:
            if delegation:
                resumed = await self._resume_delegated_approval(
                    approved=approved,
                    turn_id=turn_id,
                    delegation=delegation,
                    request_tool_approval=react_loop_kwargs["request_tool_approval"],
                    needs_tool_approval=react_loop_kwargs.get("needs_tool_approval"),
                )
                if resumed.get("waiting_approval"):
                    self._last_pending_approval_id = resumed["approval_id"]
                    return resumed["content"]
                tool_message = resumed["tool_message"]
            elif approved:
                tool = self._approval_tool_map().get(tool_name)
                cached = await self._session_store.get_tool_result(turn_id, tool_call_id)
                if cached is not None:
                    tool_message = ToolMessage(content=cached, tool_call_id=tool_call_id)
                elif tool is None:
                    tool_message = ToolMessage(
                        content=(f"[ERROR] Approved tool '{tool_name}' is no longer available after restart."),
                        tool_call_id=tool_call_id,
                    )
                else:
                    with execution_scope(self._execution_guard(thread_id, turn_id)):
                        tool_message = await execute_tool(
                            tool,
                            {"name": tool_name, "id": tool_call_id, "args": args},
                            begin_external_effect=react_loop_kwargs["begin_external_effect"],
                            finish_external_effect=react_loop_kwargs["finish_external_effect"],
                            turn_id=turn_id,
                        )
                    await self._session_store.save_tool_result(
                        turn_id=turn_id,
                        tool_call_id=tool_call_id,
                        content=str(tool_message.content),
                    )
            else:
                tool_message = ToolMessage(
                    content="[REJECTED by user]",
                    tool_call_id=tool_call_id,
                )

            await self._session_store.append_turn_messages(
                turn_id=turn_id,
                thread_id=thread_id,
                messages=[tool_message],
            )
            messages.append(tool_message)
        except ExecutionStoppedError as error:
            await self._session_store.fail_turn(turn_id, str(error))
            raise
        except DurableStateError:
            await self._session_store.fail_turn(turn_id, "durable effect or journal failure; review required")
            raise

        audit_token = set_tool_audit_context(
            agent=settings.agent_name,
            thread_id=thread_id,
            user_id=decided_by,
            session_id=thread_id,
            source_kind="approval_callback",
        )
        try:
            try:
                result = await self._run_guarded_model_loop(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    messages=messages,
                    source_message=str(pending.get("input_message", "")),
                    react_loop_kwargs=react_loop_kwargs,
                )
            except Exception as e:
                await self._session_store.fail_turn(turn_id, str(e))
                raise
        finally:
            reset_tool_audit_context(audit_token)

        if getattr(result, "waiting_approval", False):
            self._last_pending_approval_id = result.approval_id
            return result.content

        save_messages = [message for message in result.messages if not isinstance(message, SystemMessage)]
        await self._session_store.finalize_turn(
            thread_id=thread_id,
            messages=save_messages,
            turn_id=turn_id,
            content=result.content,
            failure_reason=getattr(result, "failure_reason", ""),
        )
        return result.content

    async def resume_interrupted_turn(
        self,
        turn: dict | str,
        *,
        max_attempts: int | None = None,
        notify: bool = True,
        execution_ownership: TurnOwnership | None = None,
    ) -> str | None:
        """Exclusively resume an abandoned turn using its durable identity.

        A caller-supplied snapshot is never execution authority. Status and
        attempts are checked again after acquiring the conversation lock.
        Raises TurnBusyError rather than stealing a live executor's work.
        """
        if not self._session_store:
            return None
        turn_id = str(turn.get("turn_id") or "") if isinstance(turn, dict) else turn
        detail = await self._session_store.get_turn_detail(turn_id)
        if not detail:
            return None
        if detail["thread_id"].startswith("plan:"):
            from kronos import plans

            if not plans.plan_for_turn(turn_id, settings.agent_name):
                return None
        if max_attempts is None:
            from kronos.policy import get_policy

            max_attempts = get_policy().durable.max_resume_attempts

        async def resume_owned(ownership: TurnOwnership) -> str | None:
            ownership.assert_held(self._session_store.db_path, detail["thread_id"])
            claimed = await self._session_store.claim_turn_for_resume(
                turn_id,
                ownership=ownership,
                max_attempts=max_attempts,
                notify=notify and not detail["thread_id"].startswith("plan:"),
            )
            if not claimed:
                return None
            if detail["thread_id"].startswith("plan:"):
                plans.note_turn_resuming(turn_id, settings.agent_name)
            return await self._resume_claimed_turn(claimed)

        if execution_ownership is not None:
            return await resume_owned(execution_ownership)
        async with own_conversation(self._session_store.db_path, detail["thread_id"], wait=False) as ownership:
            return await resume_owned(ownership)

    async def _resume_claimed_turn(self, turn: dict) -> str | None:
        """Finish a turn whose process died mid-flight.

        An unresolved effect intent blocks automatic continuation before any
        model call. Recorded effects can be reused by engine-managed tools.
        Caller holds exclusive conversation ownership through finalization.
        """
        if not self._session_store:
            return None

        turn_id = str(turn.get("turn_id") or "")
        thread_id = str(turn.get("thread_id") or "")
        if not turn_id or not thread_id:
            return None

        if thread_id.startswith("plan:"):
            from kronos import plans

            plan = plans.plan_for_turn(turn_id, settings.agent_name)
            if not plan or thread_id != f"plan:{plan['id']}":
                await self._session_store.fail_turn(turn_id, "plan_not_authorized_to_resume")
                return None

        try:
            await self._session_store.assert_turn_effects_settled(turn_id)
        except DurableStateError:
            await self._session_store.fail_turn(turn_id, "unresolved external effect; reconciliation required")
            return None

        try:
            messages = await self._session_store.load_turn_messages(thread_id, turn_id)
        except DurableStateError:
            await self._session_store.fail_turn(turn_id, "invalid durable journal; review required")
            return None
        if not messages:
            await self._session_store.fail_turn(turn_id, "nothing to resume")
            return None

        react_loop_kwargs = self._build_durable_react_loop_kwargs(
            turn_id=turn_id,
            thread_id=thread_id,
        )
        react_loop_kwargs["resume_pending_tools"] = True
        audit_token = set_tool_audit_context(
            agent=settings.agent_name,
            thread_id=thread_id,
            session_id=thread_id,
            source_kind="durable_resume",
        )
        try:
            result = await self._run_guarded_model_loop(
                thread_id=thread_id,
                turn_id=turn_id,
                messages=messages,
                source_message=str(turn.get("input_message") or ""),
                react_loop_kwargs=react_loop_kwargs,
            )
        except ExecutionStoppedError as error:
            await self._session_store.fail_turn(turn_id, str(error))
            return None
        except Exception as e:
            log.error("Resume failed for turn %s: %s", turn_id, e)
            await self._session_store.fail_turn(turn_id, f"resume failed: {e}")
            return None
        finally:
            reset_tool_audit_context(audit_token)

        if getattr(result, "waiting_approval", False):
            # The resumed turn hit an approval gate: leave it pending, exactly as
            # a first-time run would.
            self._last_pending_approval_id = result.approval_id
            return result.content

        save_messages = [message for message in result.messages if not isinstance(message, SystemMessage)]
        await self._session_store.finalize_turn(
            thread_id=thread_id,
            messages=save_messages,
            turn_id=turn_id,
            content=result.content,
            failure_reason=getattr(result, "failure_reason", ""),
        )
        log.info("Resumed interrupted turn %s (attempt %s)", turn_id, turn.get("attempts"))
        return result.content

    async def resume_abandoned_turns(self, *, notify: bool = True, max_attempts: int = 2) -> int:
        """Resume abandoned work; result/approval delivery is owned by SQLite.

        No transport callback runs after finalization. Recovery records its duty
        before the model call, and the independent worker drains even completed
        turns after a restart. Legacy destinations remain explicitly unknown.
        """
        if not self._session_store:
            return 0
        finished = 0
        for turn in await self._session_store.resumable_turns():
            try:
                await self.resume_interrupted_turn(turn, max_attempts=max_attempts, notify=notify)
            except TurnBusyError:
                continue
            outcome = await self.get_turn_outcome(turn["turn_id"])
            if outcome.status == "completed":
                finished += 1
        return finished

    async def _resume_delegated_approval(
        self,
        *,
        approved: bool,
        turn_id: str,
        delegation: dict,
        request_tool_approval,
        needs_tool_approval,
    ) -> dict:
        """Resume an approval that fired inside a sub-agent.

        The approval-worthy tool lives in a sub-agent, not at the top level, so
        it can't be executed directly here (it isn't in the approval tool map,
        and running it in isolation would drop the sub-agent's remaining work).
        Instead re-run the parent ``delegate_to_X`` call with the approved
        sub-call exempted; the sub-agent's completed result fills the delegation
        call's ToolMessage. A rejection short-circuits the delegation. If the
        sub-agent hits a *different* approval-worthy tool on the re-run, it
        pauses again (returned as ``waiting_approval``).

        Returns ``{"tool_message": ToolMessage}`` or
        ``{"waiting_approval": True, "approval_id": ..., "content": ...}``.
        """
        call_id = str(delegation.get("tool_call_id", ""))
        deleg_name = str(delegation.get("tool_name", ""))
        request = str(delegation.get("request", ""))

        if not approved:
            return {"tool_message": ToolMessage(content="[REJECTED by user]", tool_call_id=call_id)}

        deleg_tool = self._approval_tool_map().get(deleg_name)
        if deleg_tool is None:
            return {
                "tool_message": ToolMessage(
                    content=f"[ERROR] Delegation tool '{deleg_name}' is no longer available after restart.",
                    tool_call_id=call_id,
                )
            }

        # Publish the exemption + approval channel so the re-run sub-agent
        # executes the approved call (and can pause anew for a different one).
        turn_detail = await self._session_store.get_turn_outcome(turn_id)
        if not turn_detail.thread_id:
            raise DurableStateError("approval turn identity missing")
        durable = self._build_durable_react_loop_kwargs(turn_id=turn_id, thread_id=turn_detail.thread_id)
        ctx_token = publish_delegation_ctx(
            {
                "begin_external_effect": durable["begin_external_effect"],
                "finish_external_effect": durable["finish_external_effect"],
                "turn_id": turn_id,
                "request_tool_approval": request_tool_approval,
                "needs_tool_approval": needs_tool_approval,
                "tool_name": deleg_name,
                "tool_call_id": call_id,
                "request": request,
            }
        )
        try:
            with execution_scope(self._execution_guard(turn_detail.thread_id, turn_id)):
                tool_message = await execute_tool(
                    deleg_tool,
                    {"name": deleg_name, "id": call_id, "args": {"request": request}},
                )
        except SubAgentApprovalPause as pause:
            return {
                "waiting_approval": True,
                "approval_id": pause.approval_id,
                "content": (
                    "⚠️ Нужно ещё одно подтверждение перед выполнением tool-call.\n"
                    f"Tool: `{pause.tool_name}`\n"
                    f"Approval ID: `{pause.approval_id}`\n\n"
                    "Нажми Approve/Reject в Telegram или обработай approval вручную."
                ),
            }
        finally:
            clear_delegation_ctx(ctx_token)

        await self._session_store.save_tool_result(
            turn_id=turn_id,
            tool_call_id=call_id,
            content=str(tool_message.content),
        )
        return {"tool_message": tool_message}

    async def ainvoke(
        self,
        message: str,
        thread_id: str,
        user_id: str = "",
        session_id: str = "",
        source_kind: str = "user",
        persist_user_turn: bool = True,
        extra_system_context: str = "",
        on_tool_event: ToolEventCallback | None = None,
        force_tier: str | None = None,
        recovery_destination: RecoveryDestination | None = None,
    ) -> str:
        """Process a message, preserving the legacy text-only API.

        For execution state and a call-local approval id use ``ainvoke_outcome``.
        """
        outcome = await self.ainvoke_outcome(
            message=message,
            thread_id=thread_id,
            user_id=user_id,
            session_id=session_id,
            source_kind=source_kind,
            persist_user_turn=persist_user_turn,
            extra_system_context=extra_system_context,
            on_tool_event=on_tool_event,
            force_tier=force_tier,
            recovery_destination=recovery_destination,
        )
        return outcome.content

    async def get_turn_outcome(self, turn_id: str) -> InvocationOutcome:
        """Read a durable outcome without resuming or repeating the turn."""
        if self._session_store is None:
            return InvocationOutcome("unknown", turn_id=turn_id, reason="no_session_store")
        return await self._session_store.get_turn_outcome(turn_id)

    async def ainvoke_outcome(
        self,
        message: str,
        thread_id: str,
        user_id: str = "",
        session_id: str = "",
        source_kind: str = "user",
        persist_user_turn: bool = True,
        extra_system_context: str = "",
        on_tool_event: ToolEventCallback | None = None,
        force_tier: str | None = None,
        on_turn_started: Callable[[str], None] | None = None,
        caller_key: str = "",
        execution_ownership: TurnOwnership | None = None,
        recovery_destination: RecoveryDestination | None = None,
    ) -> InvocationOutcome:
        """Process raw input with call-local outcome and exclusive history writes.

        Peer reactions with persist_user_turn=False stay ephemeral. Transient
        transport framing belongs in extra_system_context, not in message.
        on_turn_started links durable work before model/tools; a failed link
        aborts execution. Durable calls serialize across processes per thread.
        """
        kwargs = dict(
            message=message,
            thread_id=thread_id,
            user_id=user_id,
            session_id=session_id,
            source_kind=source_kind,
            persist_user_turn=persist_user_turn,
            extra_system_context=extra_system_context,
            on_tool_event=on_tool_event,
            force_tier=force_tier,
            recovery_destination=recovery_destination,
            on_turn_started=on_turn_started,
            caller_key=caller_key,
        )
        if execution_ownership is not None:
            if not self._session_store or not persist_user_turn:
                raise ValueError("borrowed ownership requires a durable invocation")
            execution_ownership.assert_held(self._session_store.db_path, thread_id)
            return await self._invoke_owned(**kwargs)
        if self._session_store and persist_user_turn:
            if not getattr(self, "_durable_recovery_checked", False):
                await self._session_store.recover_abandoned_turns()
                self._durable_recovery_checked = True
            async with own_conversation(self._session_store.db_path, thread_id):
                return await self._invoke_owned(**kwargs)
        return await self._invoke_owned(**kwargs)

    async def _invoke_owned(self, **kwargs) -> InvocationOutcome:
        """Attribute retrieval, model, background memory and compaction together."""
        audit_token = set_tool_audit_context(
            agent=settings.agent_name,
            thread_id=kwargs["thread_id"],
            user_id=kwargs.get("user_id", ""),
            session_id=kwargs.get("session_id", ""),
            source_kind=kwargs.get("source_kind", "user"),
        )
        try:
            with model_budget_scope(kwargs.get("force_tier")):
                return await self._invoke_execution_scoped(**kwargs)
        finally:
            reset_tool_audit_context(audit_token)

    async def _invoke_execution_scoped(self, **kwargs) -> InvocationOutcome:
        """Keep the whole plan invocation, including memory, in its stop scope."""
        thread_id = kwargs["thread_id"]
        if not thread_id.startswith("plan:"):
            return await self._invoke_scoped(**kwargs)
        if not kwargs.get("persist_user_turn", True) or self._session_store is None:
            raise DurableStateError("plan execution requires a durable turn")
        from kronos import plans

        turn_id = None
        callback = kwargs.get("on_turn_started")
        agent_name = settings.agent_name

        def guard() -> None:
            plans.assert_plan_active(thread_id, agent_name)
            if turn_id:
                plans.assert_turn_active(thread_id, turn_id, agent_name)

        def linked(value: str) -> None:
            nonlocal turn_id
            turn_id = value
            if callback is not None:
                callback(value)
            guard()

        with execution_scope(guard):
            check_execution()
            try:
                return await self._invoke_scoped(**{**kwargs, "on_turn_started": linked})
            except Exception as error:
                if turn_id:
                    await self._session_store.fail_turn(turn_id, str(error))
                raise

    async def _invoke_scoped(
        self,
        message: str,
        thread_id: str,
        user_id: str = "",
        session_id: str = "",
        source_kind: str = "user",
        persist_user_turn: bool = True,
        extra_system_context: str = "",
        on_tool_event: ToolEventCallback | None = None,
        force_tier: str | None = None,
        on_turn_started: Callable[[str], None] | None = None,
        caller_key: str = "",
        recovery_destination: RecoveryDestination | None = None,
    ) -> InvocationOutcome:
        """Process a message and return its explicit execution outcome.

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

        ``on_turn_started`` durably links a caller's work to the turn before
        any model/tool call. A failure aborts execution, never silently detaches
        the work. It requires a persistent turn and a configured session store.

        Handles the full pipeline: load history → validate → memory →
        route → store memory → compact → save history.
        """
        is_ephemeral = not persist_user_turn
        if recovery_destination is not None and (is_ephemeral or source_kind != "user" or self._session_store is None):
            raise ValueError("recovery provenance requires a durable user turn")
        if on_turn_started and (is_ephemeral or self._session_store is None):
            raise ValueError("on_turn_started requires a durable session store")
        self._last_pending_approval_id = None

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
            return InvocationOutcome("blocked", response_text, thread_id, reason="input_rejected")

        # Link durable callers before retrieval can invoke an embedding provider.
        turn_id: str | None = None
        react_loop_kwargs: dict[str, Any] = {}
        if self._session_store and not is_ephemeral:
            turn_id = await self._session_store.begin_turn(
                thread_id, message, caller_key=caller_key, recovery_destination=recovery_destination
            )
            if on_turn_started is not None:
                try:
                    on_turn_started(turn_id)
                except Exception:
                    await self._session_store.fail_turn(turn_id, "caller_turn_link_failed")
                    raise
            react_loop_kwargs = self._build_durable_react_loop_kwargs(
                turn_id=turn_id,
                thread_id=thread_id,
            )

        # Step 2: Retrieve memories (non-fatal — DB issues must not crash pipeline)
        if self._memory_enabled:
            try:
                mem_update = (
                    await run_sync_owned(retrieve_memories, state)
                    if thread_id.startswith("plan:")
                    else retrieve_memories(state)
                )
                if mem_update.get("messages"):
                    # Insert memory context before the last user message in
                    # working history only — never into persisted_history.
                    insert_at = len(working_history) - 1
                    for mem_msg in mem_update["messages"]:
                        working_history.insert(insert_at, mem_msg)
                        insert_at += 1
            except DurableStateError:
                raise
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
            try:
                result = await self._run_guarded_model_loop(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    messages=working_history,
                    source_message=message,
                    react_loop_kwargs=react_loop_kwargs,
                    on_tool_event=on_tool_event,
                    force_tier=force_tier,
                )
                response_text = result.content
            except Exception as e:
                if self._session_store and turn_id:
                    await self._session_store.fail_turn(turn_id, str(e))
                raise
        finally:
            reset_tool_audit_context(audit_token)

        if getattr(result, "waiting_approval", False):
            self._last_pending_approval_id = result.approval_id
            return InvocationOutcome(
                "waiting_approval",
                response_text,
                thread_id,
                turn_id,
                result.approval_id,
            )

        if not is_ephemeral:
            persisted_history.append(AIMessage(content=response_text))

        # Step 4: Store memories — skip entirely for ephemeral peer reactions.
        if self._memory_enabled and not is_ephemeral:
            mem_state = {**state, "messages": list(persisted_history)}
            if thread_id.startswith("plan:"):
                await run_sync_owned(store_memories_background, mem_state)
            else:
                asyncio.get_running_loop().run_in_executor(None, copy_context().run, store_memories_background, mem_state)

        # Step 5: Compact if needed (only for real user turns).
        #
        # Not conditional on memory: keeping a conversation sendable is context
        # management, and it used to be skipped entirely wherever Mem0 was not
        # configured — so the deployments with no long-term memory were also the
        # ones carrying unbounded histories. The engines that flush to memory
        # already cope with it being unavailable.
        if not is_ephemeral:
            engine = get_context_engine()
            compact_state = {**state, "messages": list(persisted_history)}
            if engine.should_compact(compact_state):
                compact_result = (
                    await run_sync_owned(engine.compact, compact_state)
                    if thread_id.startswith("plan:")
                    else engine.compact(compact_state)
                )
                if compact_result.get("messages"):
                    persisted_history = compact_result["messages"]

        check_execution()

        # Step 6: Save conversation history.
        if self._session_store and not is_ephemeral:
            save_messages = [m for m in persisted_history if not isinstance(m, SystemMessage)]
            if turn_id:
                await self._session_store.finalize_turn(
                    thread_id=thread_id,
                    messages=save_messages,
                    turn_id=turn_id,
                    content=response_text,
                    failure_reason=getattr(result, "failure_reason", ""),
                )
            else:
                await self._session_store.save(thread_id, save_messages)

        failure_reason = getattr(result, "failure_reason", "")
        return InvocationOutcome(
            "failed" if failure_reason else "completed",
            response_text,
            thread_id,
            turn_id,
            reason=failure_reason,
        )

    async def clear_context(self, thread_id: str) -> str:
        """Clear conversation history for a thread.

        Clears THIS conversation only. Facts the agent has learned about the
        user (Mem0 + shared_user_facts) are cross-conversation and are kept on
        purpose — the message says so rather than implying everything is gone.
        """
        if self._session_store:
            deleted = await self._session_store.clear(thread_id)
            log.info("Cleared context for thread %s: %d rows", thread_id, deleted)
            return (
                "🧹 История этого диалога очищена. "
                "Факты, которые ты мне рассказывал, я помню и дальше — "
                "они не привязаны к конкретному диалогу."
            )
        return "Session store не настроен."
