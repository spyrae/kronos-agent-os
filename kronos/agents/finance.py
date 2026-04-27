"""Finance Agent — stock prices, financial analysis, market news.

Uses: yahoo-finance MCP tools + brave-search for news.
LLM: Standard tier (analytical tasks).
"""

from langchain_core.tools import BaseTool

from kronos.engine import create_agent
from kronos.llm import ModelTier, get_model

FINANCE_TOOL_PREFIXES = {"yahoo", "stock", "finance", "market", "brave"}

FINANCE_SYSTEM_PROMPT = """Ты — Finance Agent в системе Kronos. Твоя задача — финансовый анализ, цены акций, рыночные данные.

Правила:
- Используй Yahoo Finance для актуальных цен и финансовых данных
- Используй поиск для новостей о компаниях и рынках
- Давай конкретные цифры, не общие слова
- Сравнивай метрики (P/E, revenue growth, market cap) при анализе
- Указывай дату/время данных
- Не давай инвестиционных рекомендаций напрямую — анализируй факты
- Язык: русский, тикеры и финансовые термины на английском"""


def create_finance_agent(tools: list[BaseTool], on_tool_event=None):
    """Create finance agent with market data tools."""
    finance_tools = [
        t for t in tools
        if any(prefix in t.name.lower() for prefix in FINANCE_TOOL_PREFIXES)
    ]

    if not finance_tools:
        return None

    model = get_model(ModelTier.STANDARD)

    return create_agent(
        model=model,
        tools=finance_tools,
        system_prompt=FINANCE_SYSTEM_PROMPT,
        name="finance_agent",
        on_tool_event=on_tool_event,
    )
