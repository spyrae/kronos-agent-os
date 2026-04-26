"""Task Agent — productivity: Notion, Calendar, Email, filesystem.

Uses: notion, google-workspace, filesystem MCP tools.
LLM: Lite tier (structured operations, doesn't need expensive model).
"""

from langchain_core.tools import BaseTool

from kronos.engine import create_agent
from kronos.llm import ModelTier, get_model

TASK_MCP_SERVERS = {"notion", "google-workspace", "filesystem"}
TASK_TOOL_PREFIXES = {"notion", "google", "workspace", "gmail", "calendar", "filesystem", "read_file", "write_file", "list"}

TASK_SYSTEM_PROMPT = """Ты — Task Agent в системе Kronos. Твоя задача — управление задачами, календарём, почтой и файлами.

## Notion — ключевые базы данных

### Expenses (затраты)
Database ID: `{NOTION_EXPENSES_DB_ID}`

Для создания расхода используй tool `API-post-page` с таким payload:
```json
{
  "parent": {"database_id": "{NOTION_EXPENSES_DB_ID}"},
  "properties": {
    "Description": {"title": [{"text": {"content": "описание расхода"}}]},
    "Date": {"date": {"start": "2026-04-09"}},
    "Amount_IDR": {"number": 100000},
    "Category": {"select": {"name": "Food"}},
    "Split": {"checkbox": false},
    "Rate": {"number": 5.80},
    "Amount_RUB": {"number": 580},
    "Ref": {"rich_text": [{"text": {"content": "ref-id"}}]},
    "Status": {"select": {"name": "Processed"}}
  }
}
```

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


def create_task_agent(tools: list[BaseTool]):
    """Create task agent with productivity tools."""
    task_tools = [
        t for t in tools
        if (t.metadata or {}).get("mcp_server") in TASK_MCP_SERVERS
        or any(prefix in t.name.lower() for prefix in TASK_TOOL_PREFIXES)
    ]

    if not task_tools:
        return None

    model = get_model(ModelTier.STANDARD)

    return create_agent(
        model=model,
        tools=task_tools,
        system_prompt=TASK_SYSTEM_PROMPT,
        name="task_agent",
    )
