"""Dynamic tool management — agent-facing tools for creating new tools."""

import logging

from langchain_core.tools import tool

from kronos.tools.dynamic import TOOLS_DIR, create_tool

log = logging.getLogger("kronos.tools.dynamic_tools")


@tool
async def create_new_tool(name: str, description: str) -> str:
    """Create a new tool from a natural language description.
    The tool will be generated as Python code, validated for safety,
    and registered for use. It persists across restarts.

    Args:
        name: Tool name in snake_case (e.g. 'currency_converter')
        description: What the tool should do (e.g. 'Convert between currencies using fixed rates')
    """
    result_tool, message = await create_tool(name, description)
    return message


@tool
def list_dynamic_tools() -> str:
    """List all dynamically created tools."""
    if not TOOLS_DIR.exists():
        return "No dynamic tools created yet."

    tools = list(TOOLS_DIR.glob("*.py"))
    if not tools:
        return "No dynamic tools created yet."

    lines = ["Dynamic tools:"]
    for t in sorted(tools):
        name = t.stem
        first_line = ""
        content = t.read_text(encoding="utf-8")
        # Extract docstring
        if content.startswith('"""'):
            end = content.find('"""', 3)
            if end > 0:
                first_line = content[3:end].strip()[:80]
        lines.append(f"  - {name}: {first_line}")

    return "\n".join(lines)


def get_dynamic_management_tools() -> list:
    """Get tools for managing dynamic tools."""
    return [create_new_tool, list_dynamic_tools]
