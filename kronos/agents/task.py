"""Task Agent — productivity: Notion, Calendar, Email, filesystem.

Uses: notion, google-workspace, filesystem MCP tools.
LLM: Lite tier (structured operations, doesn't need expensive model).
"""

import os

from langchain_core.tools import BaseTool

from kronos.engine import create_agent
from kronos.llm import ModelTier, get_model

TASK_MCP_SERVERS = {"notion", "google-workspace", "filesystem"}
TASK_TOOL_PREFIXES = {"notion", "google", "workspace", "gmail", "calendar", "filesystem", "read_file", "write_file", "list"}

TASK_SYSTEM_PROMPT = """Ты — Task Agent в системе Kronos. Твоя задача — управление задачами, календарём, почтой и файлами.

## Notion — ключевые базы данных

### Expenses (затраты)
Database ID: `{NOTION_EXPENSES_DB_ID}`

НЕ создавай расходы через Notion MCP/API-post-page. Создание расходов выполняет
только прямой supervisor tool `add_expense`, потому что он конвертирует RUB и
обновляет FIFO-бюджет. Если сюда делегировали создание расхода — верни ошибку
и попроси вызвать `add_expense`.

Категории: Food, Transport, Subscriptions, Shopping, Travel, Health, Entertainment, Other.
Валюты: IDR (Amount_IDR), MYR (Amount_MYR). Amount_RUB = сумма в рублях.
Split = true → сумма делится пополам.

Для запроса расходов используй `API-query-data-source` с database_id.

## Правила
- Создавай, обновляй и проверяй задачи в Notion
- Управляй календарём и событиями
- Отправляй и читай email
- Работай с файлами в workspace
- При цепочках операций — выполняй последовательно
- Подтверждай выполнение каждого шага
- Язык: русский"""


def create_task_agent(tools: list[BaseTool], on_tool_event=None):
    """Create task agent with productivity tools."""
    task_tools = [
        t for t in tools
        if (t.metadata or {}).get("mcp_server") in TASK_MCP_SERVERS
        or any(prefix in t.name.lower() for prefix in TASK_TOOL_PREFIXES)
    ]

    if not task_tools:
        return None

    model = get_model(ModelTier.STANDARD)
    system_prompt = TASK_SYSTEM_PROMPT.replace(
        "{NOTION_EXPENSES_DB_ID}",
        os.environ.get("NOTION_EXPENSES_DB_ID", ""),
    )

    return create_agent(
        model=model,
        tools=task_tools,
        system_prompt=system_prompt,
        name="task_agent",
        on_tool_event=on_tool_event,
    )
