"""Composio integration — 250+ app integrations via Composio SDK.

Composio provides pre-built tools for Slack, Jira, GitHub, Gmail,
Google Calendar, Notion, Trello, Asana, Linear, etc.

Requires: composio-langchain package + COMPOSIO_API_KEY.
"""

import logging
from langchain_core.tools import BaseTool

from kronos.config import settings

log = logging.getLogger("kronos.tools.composio")


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
        return tools

    except Exception as e:
        log.error("Composio initialization failed: %s", e)
        return []
