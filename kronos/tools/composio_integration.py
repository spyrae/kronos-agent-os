"""Composio integration — 250+ app integrations via Composio SDK.

Composio provides pre-built tools for Slack, Jira, GitHub, Gmail,
Google Calendar, Notion, Trello, Asana, Linear, etc.

Requires: composio-langchain package + COMPOSIO_API_KEY.
"""

import logging

from langchain_core.tools import BaseTool

from kronos.config import settings
from kronos.security.mcp_tools import mcp_requires_approval, normalized_tool_name
from kronos.security.untrusted import mark_untrusted

log = logging.getLogger("kronos.tools.composio")


def classify_composio_tools(tools: list[BaseTool]) -> list[BaseTool]:
    """Give third-party actions the same local policy as MCP tools.

    Composio names lead with the app (``GITHUB_GET_ISSUE``), so the action is
    judged without that prefix. Anything not recognisably a read is an effect:
    it needs approval and goes through the intent ledger. What these tools
    return is external content and is framed as untrusted.
    """
    marked = mark_untrusted(tools, reason="composio")
    for tool in marked:
        action = normalized_tool_name(tool.name).split("_", 1)[-1]
        risky = mcp_requires_approval(action)
        tool.metadata = {**(tool.metadata or {}), "needs_approval": risky, "side_effect": risky}
    return marked


def get_composio_tools(apps: list[str] | None = None) -> list[BaseTool]:
    """Get Composio tools for specified apps.

    Args:
        apps: List of app names (e.g. ['SLACK', 'GITHUB', 'LINEAR']).
              If None, loads all connected apps.

    Returns empty list if composio is not installed or not configured.
    """
    if not getattr(settings, "composio_api_key", ""):
        log.info("Composio disabled: COMPOSIO_API_KEY not set")
        return []

    try:
        from composio_langchain import ComposioToolSet
    except ImportError:
        log.info("Composio disabled: composio-langchain not installed (pip install composio-langchain)")
        return []

    try:
        toolset = ComposioToolSet(api_key=settings.composio_api_key)

        if apps:
            # Import app enum
            from composio_langchain import App

            app_enums = []
            for app_name in apps:
                app_enum = getattr(App, app_name.upper(), None)
                if app_enum:
                    app_enums.append(app_enum)
                else:
                    log.warning("Unknown Composio app: %s", app_name)

            if not app_enums:
                return []

            tools = toolset.get_tools(apps=app_enums)
        else:
            # Get all connected app tools
            tools = toolset.get_tools()

        log.info("Composio: loaded %d tools from %s", len(tools), apps or "all connected")
        return classify_composio_tools(tools)

    except Exception as e:
        log.error("Composio initialization failed: %s", e)
        return []
