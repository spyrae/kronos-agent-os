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

import logging
from typing import Callable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool, StructuredTool

from kronos.agents.analytics import create_analytics_agent
from kronos.agents.competitor_monitor import create_competitor_monitor_agent
from kronos.agents.server_ops import create_server_ops_agent
from kronos.agents.deep_research.graph import create_deep_research_agent
from kronos.agents.finance import create_finance_agent
from kronos.agents.research import create_research_agent
from kronos.agents.task import create_task_agent
from kronos.agents.telegram_channels import create_telegram_channels_agent
from kronos.agents.topic_research.graph import create_topic_research_agent
from kronos.engine import AgentResult, react_loop
from kronos.llm import ModelTier, get_model
from kronos.config import settings

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
3. НЕ считай рубли сам — tool конвертирует автоматически по FIFO.
4. НЕ передавай параметр `date` — tool сам поставит сегодняшнюю дату. Передавай date ТОЛЬКО если пользователь ЯВНО указал другую дату.
5. Передай: description, amount, currency="IDR", category. Всё.
6. После вызова tool — перескажи результат пользователю ДОСЛОВНО, не перефразируй.
7. Если tool вернул ошибку — перескажи ошибку, не говори "записал".

### Notion (не расходы), задачи, календарь, email, файлы → delegate_to_task

### Остальные правила
1. Если запрос соответствует одному из скиллов — сначала `load_skill(skill_name)` для протокола.
2. Если запрос требует ГЛУБОКОГО исследования — delegate_to_deep_research
3. Если запрос о темах для блога — delegate_to_topic_research
4. Если запрос требует быстрого поиска в интернете — delegate_to_research
5. Если запрос о ценах акций, финансах, инвестициях — delegate_to_finance
6. Если запрос о Telegram-каналах — delegate_to_telegram_channels
7. Если запрос о конкурентах, App Store, мониторинге конкурентов — delegate_to_competitor_monitor
8. Если запрос о серверах, логах, рестарте сервисов, ошибках на сервере, диске, SSH, "что упало", "рестартни", swarm.db — delegate_to_server_ops
9. Если запрос о метриках продукта, пользователях, DAU, рейтинге, трафике, health, status, pulse — delegate_to_analytics
10. Если запрос простой (приветствие, вопрос, размышление) — отвечай сам
11. Если сложный запрос — вызывай агентов/скиллы последовательно, потом синтезируй

{persona_context}"""


def _make_delegation_tool(agent_name: str, description: str, agent_fn: Callable) -> StructuredTool:
    """Create a delegation tool that routes to a sub-agent."""

    async def delegate(request: str) -> str:
        """Delegate request to specialized agent.

        Args:
            request: The full user request to delegate.
        """
        try:
            result = await agent_fn([HumanMessage(content=request)])
            return result.content
        except Exception as e:
            log.error("Agent '%s' failed: %s", agent_name, e)
            return f"Агент {agent_name} недоступен: {e}"

    tool_name = f"delegate_to_{agent_name}"
    return StructuredTool.from_function(
        coroutine=delegate,
        name=tool_name,
        description=description,
    )


def build_supervisor(tools: list[BaseTool]):
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
        deep_research = create_deep_research_agent(tools)
        delegation_tools.append(_make_delegation_tool(
            "deep_research",
            'Глубокое исследование (multi-step: plan → search → evaluate → synthesize). '
            'Для "исследуй", "research", "проверь идею", "анализ рынка", "тренды"',
            deep_research,
        ))
        descriptions.append(
            "- **delegate_to_deep_research**: глубокое исследование "
            '(multi-step). Для "исследуй", "research", "проверь идею"'
        )
        log.info("Deep Research agent created")
    except Exception as e:
        log.warning("Deep Research agent failed to create: %s", e)

    # Topic Research — blog topic discovery pipeline
    try:
        topic_research = create_topic_research_agent(tools)
        delegation_tools.append(_make_delegation_tool(
            "topic_research",
            'Поиск и валидация тем для блога. '
            'Для "найди темы", "topic research", "blog topics", "контент-план"',
            topic_research,
        ))
        descriptions.append(
            '- **delegate_to_topic_research**: поиск тем для блога. '
            'Для "найди темы", "topic research", "контент-план"'
        )
        log.info("Topic Research agent created")
    except Exception as e:
        log.warning("Topic Research agent failed to create: %s", e)

    # Research — quick web search
    research = create_research_agent(tools)
    if research:
        delegation_tools.append(_make_delegation_tool(
            "research",
            "Быстрый поиск в интернете, извлечение контента, анализ источников",
            research,
        ))
        descriptions.append("- **delegate_to_research**: быстрый поиск в интернете")
        log.info("Research agent created")

    # Task — productivity (ONLY way to write to Notion)
    task = create_task_agent(tools)
    if task:
        delegation_tools.append(_make_delegation_tool(
            "task",
            "ЕДИНСТВЕННЫЙ способ работать с Notion, расходами, задачами, календарём, email, файлами. "
            "Для ЛЮБОЙ записи/чтения Notion — ОБЯЗАТЕЛЬНО вызови этот tool. Без вызова этого tool запись в Notion НЕВОЗМОЖНА.",
            task,
        ))
        descriptions.append(
            "- **delegate_to_task**: ЕДИНСТВЕННЫЙ способ записать в Notion, "
            "работать с расходами, задачами, календарём, email, файлами. "
            "Без вызова delegate_to_task запись в Notion НЕВОЗМОЖНА."
        )
        log.info("Task agent created")

    # Finance — market data
    finance = create_finance_agent(tools)
    if finance:
        delegation_tools.append(_make_delegation_tool(
            "finance",
            "Финансовый анализ: цены акций, рыночные данные, метрики компаний",
            finance,
        ))
        descriptions.append("- **delegate_to_finance**: цены акций, финансовый анализ")
        log.info("Finance agent created")

    # Telegram Channels — no MCP tools needed
    try:
        tg_channels = create_telegram_channels_agent()
        delegation_tools.append(_make_delegation_tool(
            "telegram_channels",
            'Мониторинг публичных Telegram-каналов. '
            'Для "посты из канала", "дайджест каналов", "сравни каналы", "топ постов"',
            tg_channels,
        ))
        descriptions.append(
            '- **delegate_to_telegram_channels**: мониторинг публичных Telegram-каналов'
        )
        log.info("Telegram Channels agent created")
    except Exception as e:
        log.warning("Telegram Channels agent failed to create: %s", e)

    # Analytics — infra health, metrics, on-demand queries
    try:
        analytics = create_analytics_agent()
        delegation_tools.append(_make_delegation_tool(
            "analytics",
            'Аналитика: инфра, продукт, пользователи, App Store, трафик, health check. '
            'Для "серверы", "ошибки", "пользователи", "DAU", "рейтинг", "App Store", '
            '"трафик", "health", "status", "метрики", "pulse", "как дела", "как продукт"',
            analytics,
        ))
        descriptions.append(
            '- **delegate_to_analytics**: инфра, продукт, пользователи, App Store, трафик, daily pulse'
        )
        log.info("Analytics agent created")
    except Exception as e:
        log.warning("Analytics agent failed to create: %s", e)

    # Competitor Monitor — no MCP tools needed
    try:
        competitor_monitor = create_competitor_monitor_agent()
        delegation_tools.append(_make_delegation_tool(
            "competitor_monitor",
            'Мониторинг конкурентов: App Store/Play Store данные, изменения, дайджест. '
            'Для "конкуренты", "competitors", "что нового у Wanderlog", "competitor check"',
            competitor_monitor,
        ))
        descriptions.append(
            '- **delegate_to_competitor_monitor**: мониторинг конкурентов '
            '(App Store, Play Store, изменения, дайджест)'
        )
        log.info("Competitor Monitor agent created")
    except Exception as e:
        log.warning("Competitor Monitor agent failed to create: %s", e)

    # Server Ops — SSH-based server diagnostics and management
    try:
        server_ops = create_server_ops_agent()
        delegation_tools.append(_make_delegation_tool(
            "server_ops",
            'Диагностика и управление серверами через SSH: логи, статус сервисов, ошибки, '
            'рестарт, диск, swarm.db. Для "серверы", "логи", "рестартни", "ошибки на сервере", '
            '"что упало", "диск забит", "swarm.db"',
            server_ops,
        ))
        descriptions.append(
            '- **delegate_to_server_ops**: SSH-диагностика серверов '
            '(логи, статус, ошибки, рестарт сервисов, диск, swarm.db)'
        )
        log.info("Server Ops agent created")
    except Exception as e:
        log.warning("Server Ops agent failed to create: %s", e)

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

    model = get_model(ModelTier.STANDARD)

    # Supervisor-only tools: skills, gateway, dynamic tools
    SUPERVISOR_TOOL_NAMES = {
        "load_skill", "load_skill_reference", "approve_skill",
        "mcp_add_server", "mcp_remove_server", "mcp_list_servers", "mcp_reload",
        "create_new_tool", "list_dynamic_tools",
    }
    supervisor_tools = [t for t in tools if t.name in SUPERVISOR_TOOL_NAMES]

    # Direct expense/budget tools — no delegation needed, deterministic
    from kronos.tools.expense import add_expense, add_tranche, replace_tranche, get_budget
    direct_tools = [add_expense, add_tranche, replace_tranche, get_budget]

    # Combine: delegation tools + supervisor-only tools + direct tools
    all_tools = delegation_tools + supervisor_tools + direct_tools

    async def run(messages: list[BaseMessage]) -> AgentResult:
        """Route request to appropriate sub-agent or respond directly."""
        return await react_loop(
            model=model,
            messages=list(messages),
            tools=all_tools,
            system_prompt=prompt,
            max_turns=10,  # supervisor shouldn't need many turns
        )

    run.__name__ = "supervisor"
    run.__qualname__ = "supervisor"

    log.info(
        "Supervisor created with %d delegation + %d supervisor + %d direct tools (total: %d)",
        len(delegation_tools), len(supervisor_tools), len(direct_tools), len(all_tools),
    )
    return run
