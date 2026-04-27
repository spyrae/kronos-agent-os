"""Research Agent — web search, content extraction, synthesis.

Uses: brave-search, exa, content-core, fetch, reddit MCP tools.
LLM: Standard tier (needs quality reasoning for synthesis).
"""

from langchain_core.tools import BaseTool

from kronos.engine import create_agent
from kronos.llm import ModelTier, get_model

RESEARCH_TOOL_PREFIXES = {"brave", "exa", "fetch", "content", "reddit", "extract"}

RESEARCH_SYSTEM_PROMPT = """Ты — Research Agent в системе Kronos. Твоя задача — находить информацию, анализировать источники и давать структурированные ответы.

Правила:
- Используй доступные инструменты поиска для нахождения актуальной информации
- Делай multi-hop research: если первый поиск недостаточен — уточняй запрос
- Всегда указывай источники
- Отвечай структурированно: краткий вывод, затем детали
- Обработка ошибок инструментов:
  - [SKIP] — конкретный ресурс недоступен, пропусти его и продолжай с другими
  - [SERVER DOWN] — сервер инструмента упал, переключись на альтернативу (brave-search с site:reddit.com вместо reddit и т.д.)
  - [ERROR] — неизвестная ошибка, попробуй альтернативный инструмент
- Язык: русский, технические термины на английском"""


def create_research_agent(tools: list[BaseTool], on_tool_event=None):
    """Create research agent with search/extraction tools."""
    research_tools = [
        t for t in tools
        if any(prefix in t.name.lower() for prefix in RESEARCH_TOOL_PREFIXES)
    ]

    if not research_tools:
        return None

    model = get_model(ModelTier.STANDARD)

    return create_agent(
        model=model,
        tools=research_tools,
        system_prompt=RESEARCH_SYSTEM_PROMPT,
        name="research_agent",
        on_tool_event=on_tool_event,
    )
