"""Supervisor — central routing agent that delegates to specialized sub-agents.

Routes requests to:
- deep_research_agent: multi-step deep research pipeline
- topic_research_agent: blog topic discovery pipeline
- research_agent: web search, content extraction, analysis
- task_agent: Notion, calendar, email, filesystem
- finance_agent: stock prices, market analysis
- telegram_channels_agent: public Telegram channel monitoring, digests, analytics
- Handles conversational/simple queries directly (no delegation)

No LangGraph — uses LLM tool-calling for routing decisions.
"""

import inspect
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import BaseTool, StructuredTool

from kronos.agents.analytics import create_analytics_agent
from kronos.agents.competitor_monitor import create_competitor_monitor_agent
from kronos.agents.deep_research.graph import create_deep_research_agent
from kronos.agents.finance import create_finance_agent
from kronos.agents.knowledge_pipeline.graph import create_knowledge_pipeline_agent
from kronos.agents.research import create_research_agent
from kronos.agents.task import create_task_agent
from kronos.agents.telegram_channels import create_telegram_channels_agent
from kronos.agents.topic_research.graph import create_topic_research_agent
from kronos.config import settings
from kronos.engine import (
    AgentResult,
    SubAgentApprovalPause,
    delegation_ctx,
    enter_delegation,
    exit_delegation,
    react_loop,
)
from kronos.llm import get_orchestrator_model

log = logging.getLogger("kronos.agents.supervisor")

SUPERVISOR_PROMPT = """Ты — Supervisor в системе Kronos (персона INTJ).
Твоя задача — определить, какой специализированный агент или skill лучше всего справится с запросом пользователя.

## КРИТИЧЕСКОЕ ПРАВИЛО — ЗАПРЕТ ИМИТАЦИИ

**НИКОГДА** не говори "готово", "добавлено", "записал", "сделал" если ты НЕ вызвал tool и НЕ получил успешный результат.
Если ты не можешь выполнить действие — честно скажи "не получилось" и почему.
ЛЮБАЯ имитация выполнения (когда ты говоришь что сделал, но не вызывал tool) — это КРИТИЧЕСКИЙ СБОЙ.

## Доступные агенты (делегирование)
{agent_descriptions}

## Доступные скиллы (вызывай через load_skill)
{skill_catalog}

## Правила роутинга

### Расходы → ВСЕГДА tool `add_expense`
Если запрос связан с расходами, тратами, покупками — **ОБЯЗАТЕЛЬНО** вызови tool `add_expense`.
Правила:
1. **КАЖДЫЙ расход = ОДИН вызов `add_expense`**. Без исключений.
2. НЕ делегируй расходы другим агентам. НЕ имитируй выполнение.
3. НЕ считай рубли/доллары сам для IDR — tool конвертирует автоматически по FIFO.
   Инвариант для IDR: Rate = IDR за 1 RUB, Rate_USD = IDR за 1 USD;
   Amount_RUB = round(Amount_IDR / Rate), Amount_USD = round(Amount_IDR / Rate_USD).
   Трата в рупиях конвертируется СРАЗУ и в RUB, и в USD (оба курса берутся из транша).
   Если пользователь указал рубли/₽/RUB — передай currency="RUB" (tool запишет только Amount_RUB как есть).
   Если пользователь указал доллары/$/USD — передай currency="USD" (tool запишет только Amount_USD как есть).
   RUB и USD не читают и не трогают транши.
4. НЕ передавай параметр `date` — tool сам поставит сегодняшнюю дату. Передавай date ТОЛЬКО если пользователь ЯВНО указал другую дату.
5. Передай: description, amount, currency ("IDR", "RUB" или "USD"), category. Всё.
6. После вызова tool — перескажи результат пользователю ДОСЛОВНО, не перефразируй.
7. Если tool вернул ошибку — перескажи ошибку, не говори "записал".

### Непонятные траты из почты (pending) → resolve_pending_expense / skip_pending_expense
Крон email-расходов сам присылает в этот топик вопрос «Куда отнести эти траты?» со
списком трат и их номерами `#id`, категорию которых он не смог определить.
Когда пользователь отвечает — это и есть ответ на тот вопрос. Действуй так:
1. Пользователь называет категорию для траты (например «#12 Travel», «#12 и #14 в Travel,
   #13 Food», «первую в еду», «villas — Travel») → для КАЖДОЙ упомянутой траты вызови
   `resolve_pending_expense(pending_id, category)`. Категория — одна из: Food, Transport,
   Subscriptions, Shopping, Travel, Health, Entertainment, Other.
2. «пропусти #13» / «это не расход» → `skip_pending_expense(pending_id)`.
3. «покажи pending» / «какие траты ждут» / не уверен в номерах → `list_pending_expenses()`.
4. Если непонятно, к какой именно трате относится ответ — сначала `list_pending_expenses()`,
   сопоставь по описанию, затем resolve. Каждый вызов — по одной трате.
5. После вызовов — коротко подтверди, что записал/пропустил.

### Notion (не расходы), задачи, календарь, email, файлы → delegate_to_task

### Остальные правила
1. Если запрос соответствует одному из скиллов — сначала `load_skill(skill_name)` для протокола.
2. Если запрос требует ГЛУБОКОГО исследования — delegate_to_deep_research
3. Если запрос о темах для блога — delegate_to_topic_research
4. Если запрос требует быстрого поиска в интернете — delegate_to_research
5. Если запрос о ценах акций, финансах, инвестициях — delegate_to_finance
6. Если запрос о Telegram-каналах — delegate_to_telegram_channels
7. Если запрос о конкурентах, App Store, мониторинге конкурентов — delegate_to_competitor_monitor
8. Если запрос просит записать/обработать знания, claims, wiki links, notes/inbox или "запомни как знание" — delegate_to_knowledge_pipeline
9. Если запрос о серверах, логах, рестарте сервисов, ошибках на сервере, диске, SSH, "что упало", "рестартни", swarm.db — delegate_to_server_ops
10. Если запрос о метриках продукта, пользователях, DAU, рейтинге, трафике, health, status, pulse — delegate_to_analytics
11. Если запрос простой (приветствие, вопрос, размышление) — отвечай сам
12. Если сложный запрос — вызывай агентов/скиллы последовательно, потом синтезируй

{persona_context}"""


def _accepts_approval_hooks(fn: Callable) -> bool:
    """Whether ``fn`` takes the approval-callback kwargs (create_agent's run does).

    Custom sub-agent graphs (deep_research, topic_research, knowledge_pipeline)
    have their own signatures; we don't force the kwargs on them.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return True
    return "request_tool_approval" in params


def _make_delegation_tool(agent_name: str, description: str, agent_fn: Callable) -> StructuredTool:
    """Create a delegation tool that routes to a sub-agent."""

    async def delegate(request: str) -> str:
        """Delegate request to specialized agent.

        Args:
            request: The full user request to delegate.
        """
        # If the parent turn has an approval channel, hand it to the sub-agent so
        # its approval-worthy tools pause too (instead of silently executing),
        # and record which delegate_to_X call we're inside so the resume can
        # re-run it with the approved call exempted.
        ctx = delegation_ctx()
        hooks: dict[str, Any] = {}
        active_token = None
        if ctx and ctx.get("request_tool_approval") and _accepts_approval_hooks(agent_fn):
            hooks = {
                "needs_tool_approval": ctx.get("needs_tool_approval"),
                "request_tool_approval": ctx.get("request_tool_approval"),
            }
            active_token = enter_delegation(
                {
                    "tool_name": ctx.get("tool_name", f"delegate_to_{agent_name}"),
                    "tool_call_id": ctx.get("tool_call_id", ""),
                    "request": request,
                }
            )
        try:
            result = await agent_fn([HumanMessage(content=request)], **hooks)
            if getattr(result, "waiting_approval", False):
                # The sub-agent paused for approval — bubble it up so the whole
                # turn pauses rather than returning half-done text here.
                raise SubAgentApprovalPause(result.approval_id, result.approval_tool_name or "")
            return result.content
        except SubAgentApprovalPause:
            raise
        except Exception as e:
            log.error("Agent '%s' failed: %s", agent_name, e)
            return f"Агент {agent_name} недоступен: {e}"
        finally:
            if active_token is not None:
                exit_delegation(active_token)

    tool_name = f"delegate_to_{agent_name}"
    return StructuredTool.from_function(
        coroutine=delegate,
        name=tool_name,
        description=description,
    )


def _disabled_delegation_tool_names() -> set[str]:
    """Delegation-tool names (``delegate_to_X``) the operator disabled in the
    dashboard registry (``agent_registry.json``).

    Opt-out only: an agent absent from the registry — or a missing/unreadable
    registry — disables nothing, so a stale/incomplete registry never silently
    removes agents. The path matches dashboard/api/agents.py exactly, so the
    dashboard toggle finally has a runtime consumer.
    """
    registry_file = Path(settings.db_path).parent / "agent_registry.json"
    try:
        if not registry_file.exists():
            return set()
        registry = json.loads(registry_file.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Could not read agent registry (%s); enabling all agents", e)
        return set()
    disabled: set[str] = set()
    for key, cfg in registry.items():
        if isinstance(cfg, dict) and cfg.get("enabled") is False:
            base = key[: -len("_agent")] if key.endswith("_agent") else key
            disabled.add(f"delegate_to_{base}")
    return disabled


def build_supervisor(
    tools: list[BaseTool],
    on_tool_event: Callable[[str, dict[str, Any]], None] | None = None,
):
    """Build supervisor as an async callable.

    Creates delegation tools for each sub-agent, then uses react_loop
    so the LLM can decide which agent to call (or respond directly).

    Returns an async callable(messages) -> AgentResult, or None if no agents.
    """
    delegation_tools: list[BaseTool] = []
    descriptions: list[str] = []

    # Create specialized agents and their delegation tools

    # Deep Research — multi-step pipeline
    try:
        deep_research = create_deep_research_agent(tools, on_tool_event=on_tool_event)
        delegation_tools.append(
            _make_delegation_tool(
                "deep_research",
                "Глубокое исследование (multi-step: plan → search → evaluate → synthesize). "
                'Для "исследуй", "research", "проверь идею", "анализ рынка", "тренды"',
                deep_research,
            )
        )
        descriptions.append(
            "- **delegate_to_deep_research**: глубокое исследование "
            '(multi-step). Для "исследуй", "research", "проверь идею"'
        )
        log.info("Deep Research agent created")
    except Exception as e:
        log.warning("Deep Research agent failed to create: %s", e)

    # Topic Research — blog topic discovery pipeline
    try:
        topic_research = create_topic_research_agent(tools, on_tool_event=on_tool_event)
        delegation_tools.append(
            _make_delegation_tool(
                "topic_research",
                'Поиск и валидация тем для блога. Для "найди темы", "topic research", "blog topics", "контент-план"',
                topic_research,
            )
        )
        descriptions.append(
            '- **delegate_to_topic_research**: поиск тем для блога. Для "найди темы", "topic research", "контент-план"'
        )
        log.info("Topic Research agent created")
    except Exception as e:
        log.warning("Topic Research agent failed to create: %s", e)

    # Knowledge Pipeline — durable file-handoff knowledge processing
    try:
        knowledge_pipeline = create_knowledge_pipeline_agent()
        delegation_tools.append(
            _make_delegation_tool(
                "knowledge_pipeline",
                "Обработка знаний через file handoff: notes/inbox → ops/queue task → claims → wiki links → verify → Mem0. "
                'Для "запомни как знание", "обработай knowledge", "claims", "wiki links", "notes/inbox"',
                knowledge_pipeline,
            )
        )
        descriptions.append(
            "- **delegate_to_knowledge_pipeline**: durable обработка знаний "
            "(notes/inbox, task files, claims, wiki links, verify, Mem0)"
        )
        log.info("Knowledge Pipeline agent created")
    except Exception as e:
        log.warning("Knowledge Pipeline agent failed to create: %s", e)

    # Research — quick web search
    research = create_research_agent(tools, on_tool_event=on_tool_event)
    if research:
        delegation_tools.append(
            _make_delegation_tool(
                "research",
                "Быстрый поиск в интернете, извлечение контента, анализ источников",
                research,
            )
        )
        descriptions.append("- **delegate_to_research**: быстрый поиск в интернете")
        log.info("Research agent created")

    # Task — productivity (Notion except expenses)
    task = create_task_agent(tools, on_tool_event=on_tool_event)
    if task:
        delegation_tools.append(
            _make_delegation_tool(
                "task",
                "Способ работать с Notion (кроме расходов), задачами, календарём, email, файлами. "
                "Расходы НЕ делегируй сюда: для расходов используй только прямой tool add_expense.",
                task,
            )
        )
        descriptions.append(
            "- **delegate_to_task**: работа с Notion (кроме расходов), "
            "задачами, календарём, email, файлами. "
            "Расходы запрещено делегировать: только add_expense."
        )
        log.info("Task agent created")

    # Finance — market data
    finance = create_finance_agent(tools, on_tool_event=on_tool_event)
    if finance:
        delegation_tools.append(
            _make_delegation_tool(
                "finance",
                "Финансовый анализ: цены акций, рыночные данные, метрики компаний",
                finance,
            )
        )
        descriptions.append("- **delegate_to_finance**: цены акций, финансовый анализ")
        log.info("Finance agent created")

    # Telegram Channels — no MCP tools needed
    try:
        tg_channels = create_telegram_channels_agent()
        delegation_tools.append(
            _make_delegation_tool(
                "telegram_channels",
                "Мониторинг публичных Telegram-каналов. "
                'Для "посты из канала", "дайджест каналов", "сравни каналы", "топ постов"',
                tg_channels,
            )
        )
        descriptions.append("- **delegate_to_telegram_channels**: мониторинг публичных Telegram-каналов")
        log.info("Telegram Channels agent created")
    except Exception as e:
        log.warning("Telegram Channels agent failed to create: %s", e)

    # Analytics — infra health, metrics, on-demand queries
    try:
        analytics = create_analytics_agent()
        delegation_tools.append(
            _make_delegation_tool(
                "analytics",
                "Аналитика: инфра, продукт, пользователи, App Store, трафик, health check. "
                'Для "серверы", "ошибки", "пользователи", "DAU", "рейтинг", "App Store", '
                '"трафик", "health", "status", "метрики", "pulse", "как дела", "как продукт"',
                analytics,
            )
        )
        descriptions.append("- **delegate_to_analytics**: инфра, продукт, пользователи, App Store, трафик, daily pulse")
        log.info("Analytics agent created")
    except Exception as e:
        log.warning("Analytics agent failed to create: %s", e)

    # Competitor Monitor — no MCP tools needed
    try:
        competitor_monitor = create_competitor_monitor_agent()
        delegation_tools.append(
            _make_delegation_tool(
                "competitor_monitor",
                "Мониторинг конкурентов: App Store/Play Store данные, изменения, дайджест. "
                'Для "конкуренты", "competitors", "что нового у Wanderlog", "competitor check"',
                competitor_monitor,
            )
        )
        descriptions.append(
            "- **delegate_to_competitor_monitor**: мониторинг конкурентов (App Store, Play Store, изменения, дайджест)"
        )
        log.info("Competitor Monitor agent created")
    except Exception as e:
        log.warning("Competitor Monitor agent failed to create: %s", e)

    # Server Ops — SSH-based server diagnostics and management
    try:
        if settings.enable_server_ops:
            from kronos.agents.server_ops import create_server_ops_agent

            server_ops = create_server_ops_agent(on_tool_event=on_tool_event)
        else:
            server_ops = None

        if server_ops is not None:
            delegation_tools.append(
                _make_delegation_tool(
                    "server_ops",
                    "Диагностика и управление серверами через SSH: логи, статус сервисов, ошибки, "
                    'рестарт, диск, swarm.db. Для "серверы", "логи", "рестартни", "ошибки на сервере", '
                    '"что упало", "диск забит", "swarm.db"',
                    server_ops,
                )
            )
            descriptions.append(
                "- **delegate_to_server_ops**: SSH-диагностика серверов "
                "(логи, статус, ошибки, рестарт сервисов, диск, swarm.db)"
            )
            log.info("Server Ops agent created")
        else:
            log.info("Server Ops agent disabled (ENABLE_SERVER_OPS=false)")
    except Exception as e:
        log.warning("Server Ops agent failed to create: %s", e)

    # Honor dashboard agent toggles: drop any agent the operator disabled in
    # the registry. Filter each list independently by the delegate_to_X name so
    # it is robust even if tools/descriptions ever fall out of lockstep.
    disabled_tools = _disabled_delegation_tool_names()
    if disabled_tools:
        delegation_tools = [t for t in delegation_tools if t.name not in disabled_tools]
        descriptions = [d for d in descriptions if not any(name in d for name in disabled_tools)]
        log.info("Agent registry disabled: %s", ", ".join(sorted(disabled_tools)))

    if not delegation_tools:
        log.warning("No sub-agents created, supervisor disabled")
        return None

    # Build skill catalog for supervisor
    from kronos.skills.store import SkillStore

    skill_store = SkillStore(settings.workspace_path)
    skill_catalog = skill_store.build_catalog()

    # Persona context — only core identity
    from kronos.persona import load_persona

    persona = load_persona(settings.workspace_path)
    persona_snippet = persona[:3000] if persona else ""

    prompt = SUPERVISOR_PROMPT.format(
        agent_descriptions="\n".join(descriptions),
        skill_catalog=skill_catalog,
        persona_context=persona_snippet,
    )

    model = get_orchestrator_model()

    # Supervisor-only tools: skills, gateway, dynamic tools
    SUPERVISOR_TOOL_NAMES = {
        "load_skill",
        "load_skill_reference",
        "approve_skill",
        "mcp_add_server",
        "mcp_remove_server",
        "mcp_list_servers",
        "mcp_reload",
        "create_new_tool",
        "list_dynamic_tools",
    }
    supervisor_tools = [t for t in tools if t.name in SUPERVISOR_TOOL_NAMES]

    # Direct expense/budget tools — no delegation needed, deterministic
    from kronos.tools.expense import add_expense, add_tranche, get_budget, replace_tranche
    from kronos.tools.expense_pending import (
        list_pending_expenses,
        resolve_pending_expense,
        skip_pending_expense,
    )

    direct_tools = [
        add_expense,
        add_tranche,
        replace_tranche,
        get_budget,
        list_pending_expenses,
        resolve_pending_expense,
        skip_pending_expense,
    ]

    # Combine: delegation tools + supervisor-only tools + direct tools
    all_tools = delegation_tools + supervisor_tools + direct_tools

    async def run(
        messages: list[BaseMessage],
        on_tool_event: Callable[[str, dict[str, Any]], None] | None = on_tool_event,
        **react_loop_kwargs,
    ) -> AgentResult:
        """Route request to appropriate sub-agent or respond directly.

        on_tool_event defaults to the build-time callback but can be overridden
        per call (e.g. bridge live progress), so callers layer their own sink.
        """
        return await react_loop(
            model=model,
            messages=list(messages),
            tools=all_tools,
            system_prompt=prompt,
            max_turns=10,  # supervisor shouldn't need many turns
            on_tool_event=on_tool_event,
            **react_loop_kwargs,
        )

    run.__name__ = "supervisor"
    run.__qualname__ = "supervisor"
    run._approval_tools = all_tools

    log.info(
        "Supervisor created with %d delegation + %d supervisor + %d direct tools (total: %d)",
        len(delegation_tools),
        len(supervisor_tools),
        len(direct_tools),
        len(all_tools),
    )
    return run
