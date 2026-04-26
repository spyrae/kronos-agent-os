"""Smart error handler for MCP tool failures.

Classifies errors into:
- ITEM_SPECIFIC: single resource failed (e.g. one subreddit private) → skip, try next
- SERVER_DOWN: MCP server or upstream API is broken → stop using this server
- TRANSIENT: temporary issue, may work on retry

Returns actionable guidance as ToolMessage content so the LLM knows what to do.
"""

import re

# Patterns that indicate a specific resource is unavailable (not the whole server)
_ITEM_SPECIFIC_PATTERNS = [
    re.compile(r"Cannot access r/\w+", re.IGNORECASE),
    re.compile(r"Not found - r/\w+", re.IGNORECASE),
    re.compile(r"it may be private, quarantined", re.IGNORECASE),
    re.compile(r"does not exist or is inaccessible", re.IGNORECASE),
    re.compile(r"Access forbidden - the requested content", re.IGNORECASE),
]

# Patterns that indicate the server/API itself is down
_SERVER_DOWN_PATTERNS = [
    re.compile(r"Rate limited by Reddit", re.IGNORECASE),
    re.compile(r"Reddit is temporarily unavailable", re.IGNORECASE),
    re.compile(r"Reddit returned HTML instead of JSON", re.IGNORECASE),
    re.compile(r"Reddit API error \(\d+\)", re.IGNORECASE),
    re.compile(r"ECONNREFUSED|ECONNRESET|ETIMEDOUT", re.IGNORECASE),
    re.compile(r"Connection (refused|reset|timed out)", re.IGNORECASE),
    re.compile(r"MCP server .* (crashed|disconnected|not running)", re.IGNORECASE),
    re.compile(r"spawn .* ENOENT", re.IGNORECASE),
    re.compile(r"stdio transport error", re.IGNORECASE),
]


def classify_tool_error(error: Exception) -> str:
    """Classify a tool error and return an actionable message for the LLM.

    Used as handle_tool_errors callback in ToolNode.
    """
    error_str = str(error)

    for pattern in _ITEM_SPECIFIC_PATTERNS:
        if pattern.search(error_str):
            return (
                f"[SKIP] {error_str}\n"
                "This specific resource is unavailable. "
                "Skip it and continue with the next one. "
                "The tool itself is working fine."
            )

    for pattern in _SERVER_DOWN_PATTERNS:
        if pattern.search(error_str):
            return (
                f"[SERVER DOWN] {error_str}\n"
                "This tool's server is down or rate-limited. "
                "Do NOT retry this tool — use an alternative instead "
                "(e.g. brave-search with site:reddit.com for Reddit data)."
            )

    # Unknown error — conservative: report but let LLM decide
    return (
        f"[ERROR] {error_str}\n"
        "The tool returned an unexpected error. "
        "Try an alternative tool if available."
    )
