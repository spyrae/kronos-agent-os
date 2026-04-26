"""Tests for multi-agent system."""

import pytest

pytestmark = pytest.mark.integration

from unittest.mock import patch

from langchain_core.tools import tool

from kronos.agents.finance import create_finance_agent
from kronos.agents.research import create_research_agent
from kronos.agents.task import create_task_agent


@tool
def brave_search(query: str) -> str:
    """Search the web using Brave."""
    return "search results"


@tool
def fetch_url(url: str) -> str:
    """Fetch a URL."""
    return "page content"


@tool
def notion_query(database_id: str) -> str:
    """Query Notion database."""
    return "notion results"


@tool
def yahoo_finance_price(ticker: str) -> str:
    """Get stock price from Yahoo Finance."""
    return "AAPL: $200"


@tool
def unrelated_tool(x: str) -> str:
    """A tool that doesn't match any agent."""
    return x


class TestAgentCreation:
    @patch("kronos.agents.research.get_model")
    def test_research_agent_filters_tools(self, mock_get_model):
        from unittest.mock import MagicMock
        mock_get_model.return_value = MagicMock()

        agent = create_research_agent([brave_search, fetch_url, notion_query, unrelated_tool])
        # Should be created (has matching tools)
        assert agent is not None

    @patch("kronos.agents.task.get_model")
    def test_task_agent_filters_tools(self, mock_get_model):
        from unittest.mock import MagicMock
        mock_get_model.return_value = MagicMock()

        agent = create_task_agent([notion_query, brave_search, unrelated_tool])
        assert agent is not None

    @patch("kronos.agents.finance.get_model")
    def test_finance_agent_filters_tools(self, mock_get_model):
        from unittest.mock import MagicMock
        mock_get_model.return_value = MagicMock()

        agent = create_finance_agent([yahoo_finance_price, brave_search, unrelated_tool])
        assert agent is not None

    def test_research_agent_returns_none_without_tools(self):
        agent = create_research_agent([unrelated_tool])
        assert agent is None

    def test_task_agent_returns_none_without_tools(self):
        agent = create_task_agent([unrelated_tool])
        assert agent is None

    def test_finance_agent_returns_none_without_tools(self):
        agent = create_finance_agent([unrelated_tool])
        assert agent is None


class TestKronosAgent:
    def test_agent_creates_without_tools(self):
        """When no tools → agent still works (telegram_channels creates supervisor)."""
        from kronos.graph import KronosAgent

        with patch("kronos.graph.settings") as mock:
            mock.workspace_path = "/tmp/test"
            mock.deepseek_api_key = ""
            mock.context_strategy = "summarize"
            agent = KronosAgent(tools=None, enable_supervisor=True)
            assert agent is not None
            # Supervisor may still be created (telegram_channels doesn't need MCP tools)

    def test_agent_creates_with_supervisor_disabled(self):
        """Explicitly disabled supervisor."""
        from kronos.graph import KronosAgent

        with patch("kronos.graph.settings") as mock:
            mock.workspace_path = "/tmp/test"
            mock.deepseek_api_key = ""
            mock.context_strategy = "summarize"
            agent = KronosAgent(tools=[brave_search], enable_supervisor=False)
            assert agent is not None
            assert agent._supervisor is None
