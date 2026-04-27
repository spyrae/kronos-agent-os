"""Server Ops Agent — SSH-based server diagnostics and management.

Handles:
- "серверы живы?" → service status check
- "логи импульса" → journalctl for impulse
- "ошибки за час" → error aggregation
- "рестартни nexus" → whitelisted restart
- "диск забит?" → disk usage analysis
- "что в swarm.db?" → read-only SQL queries
"""

import logging

from langchain_core.messages import BaseMessage

from kronos.config import settings
from kronos.engine import AgentResult, react_loop
from kronos.llm import ModelTier, get_model
from kronos.tools.server_ops import get_server_ops_tools

log = logging.getLogger("kronos.agents.server_ops")

SERVER_OPS_PROMPT = """Ты — Server Ops Agent. Ты диагностируешь и обслуживаешь серверы через SSH.

## Возможности

### Level 1 — Read-only (без подтверждения)
- `server_status` — uptime, load, memory, disk
- `server_service_status` — статус конкретного сервиса
- `server_all_services` — статус всех сервисов одной строкой
- `server_logs` — логи сервиса (journalctl)
- `server_errors` — ошибки за N минут
- `server_query_swarm` — read-only SQL к swarm.db
- `server_disk_detail` — детальный анализ диска

### Level 2 — Actions (whitelist)
- `server_restart_service` — рестарт сервиса (только из whitelist)
- `server_clear_journal` — очистка старых логов

## Правила

1. ВСЕГДА начинай с диагностики (Level 1), прежде чем предлагать действия (Level 2).
2. При запросе "что случилось" — проверь статус всех сервисов, потом ошибки.
3. При рестарте — СНАЧАЛА покажи текущий статус, ПОТОМ рестартни, ПОТОМ покажи новый статус.
4. Объясняй что нашёл кратко и по делу, без воды.
5. Если проблема неясна — предложи план диагностики (какие логи проверить, что посмотреть).
6. НЕ выполняй произвольные shell-команды. Только предоставленные tools.

## Инфраструктура

Вся инфраструктура берётся из `servers.yaml`. Не делай предположений о
сервере, сервисе или контейнере по имени из запроса. Всегда вызови
`server_list`, сопоставь пользовательский запрос с registry, затем работай
только с разрешёнными там сервисами и контейнерами.
"""


def create_server_ops_agent(on_tool_event=None):
    """Create server ops agent for supervisor delegation."""
    if not settings.enable_server_ops:
        return None

    tools = get_server_ops_tools()
    if not tools:
        return None

    model = get_model(ModelTier.STANDARD)

    async def run(messages: list[BaseMessage]) -> AgentResult:
        """Handle server diagnostics and management requests."""
        return await react_loop(
            model=model,
            messages=list(messages),
            tools=tools,
            system_prompt=SERVER_OPS_PROMPT,
            max_turns=15,
            on_tool_event=on_tool_event,
        )

    run.__name__ = "server_ops_agent"
    run.__qualname__ = "server_ops_agent"
    log.info("Server Ops agent created with %d tools", len(tools))
    return run
