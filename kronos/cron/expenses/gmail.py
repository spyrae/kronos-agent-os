"""Gmail adapter for the email-expenses pipeline.

Wraps the Google Workspace MCP with a **cron-scoped** client that also carries
the gmail-modify tool needed to archive processed mail — the supervisor's
``google-workspace`` server runs at ``--tool-tier core`` (read/send only) and
cannot remove labels. This client is built with ``--tools gmail`` so the
label-modify tool is available for archiving.

Three intents used by the processor:

  * ``search(query, limit)`` → list of ``{message_id, thread_id}`` refs
  * ``fetch(message_ids)``   → list of :class:`EmailMessage` (full text for the LLM)
  * ``archive(message_id)``  → remove the ``INBOX`` label (Gmail "Archive")

The Workspace MCP returns results as text blocks (``[{"type": "text",
"text": ...}]``), not structured JSON: search emits a listing with
``Message ID:`` / ``Thread ID:`` lines, and content emits a ``Subject:/From:/
--- BODY ---`` blob that is fed to the LLM as-is. Tool arguments follow the
server's schema exactly (``query`` + ``user_google_email`` + ``page_size`` for
search); calls degrade gracefully (log + empty/False) instead of raising.

Archiving needs the OAuth grant to include the ``gmail.modify`` scope. Without it
the modify tool is absent or returns an error — the processor keeps the expense
recorded and leaves the email in the inbox rather than failing.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from kronos.config import settings

log = logging.getLogger("kronos.cron.expenses.gmail")

# Preferred exact tool names, with term-based fallback for MCP version drift.
_SEARCH_TOOL, _SEARCH_TERMS = "search_gmail_messages", ("search", "find", "list")
_READ_TOOL, _READ_TERMS = "get_gmail_message_content", ("content", "get", "read", "fetch")
_MODIFY_TOOL, _MODIFY_TERMS = "modify_gmail_message_labels", ("modify", "label", "archive", "trash")
_MAIL_TERMS = ("gmail", "mail", "message")

_MAX_BODY_CHARS = 4000

# Markers specific to MCP/tool failures — chosen so they never appear in a
# legitimate email body (so fetched content is not misclassified as an error).
_ERROR_MARKERS = (
    "validation error",
    "invalid arguments for tool",
    "missing required argument",
    "unexpected keyword argument",
    "authentication fail",
    "no authenticated user",
)

_MSG_ID_RE = re.compile(r"Message ID:\s*(\S+)")
_THREAD_ID_RE = re.compile(r"Thread ID:\s*(\S+)")


@dataclass
class EmailMessage:
    """One fetched email, ready for LLM extraction.

    ``text`` is the full human-readable content (subject + sender + date + body)
    — we do NOT pre-parse amounts/dates in Python; the LLM does that. ``source``
    is a coarse origin guess (permata|wondr|grab|other) used for labelling and
    dedup hints, not for parsing.
    """

    message_id: str
    text: str
    source: str = "other"
    thread_id: str = ""


def _build_gmail_mcp_config() -> dict | None:
    """Cron-scoped google-workspace MCP config that includes gmail-modify."""
    if not (settings.google_oauth_client_id and settings.google_oauth_client_secret):
        return None

    from kronos.tools.mcp_servers import _find_uvx

    # ``--tools gmail`` exposes the whole Gmail group (search + read + modify),
    # unlike the supervisor's ``--tool-tier core`` which omits label modify.
    tools_arg = os.environ.get("EXPENSES_GMAIL_MCP_TOOLS", "gmail").strip() or "gmail"
    return {
        "transport": "stdio",
        "command": _find_uvx(),
        "args": ["workspace-mcp", "--tools", *tools_arg.split()],
        "env": {
            "GOOGLE_OAUTH_CLIENT_ID": settings.google_oauth_client_id,
            "GOOGLE_OAUTH_CLIENT_SECRET": settings.google_oauth_client_secret,
        },
    }


def _result_text(result: Any) -> str:
    """Flatten an MCP tool result into plain text.

    Handles the Workspace MCP shape ``[{"type": "text", "text": ...}]`` as well
    as bare strings and ``{"text": ...}`` dicts.
    """
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return str(result.get("text", result))
    if isinstance(result, (list, tuple)):
        parts = []
        for item in result:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    return str(result)


def _looks_like_error(text: str) -> bool:
    low = text.casefold()
    return any(marker in low for marker in _ERROR_MARKERS)


def _find_tool(tools: Sequence[Any], exact: str, terms: Sequence[str]) -> Any | None:
    """Return the tool by exact name, else the first mail-ish name matching terms."""
    for tool in tools:
        if getattr(tool, "name", "") == exact:
            return tool
    required = tuple(t.casefold() for t in terms)
    for tool in tools:
        haystack = f"{getattr(tool, 'name', '')} {getattr(tool, 'description', '')}".casefold()
        if any(m in haystack for m in _MAIL_TERMS) and any(t in haystack for t in required):
            return tool
    return None


class GmailClient:
    """Best-effort Gmail access over the cron-scoped Google Workspace MCP."""

    def __init__(self, account: str, config: dict | None = None):
        self._account = account
        self._config = config
        self._tools: list[Any] | None = None

    async def _load_tools(self) -> list[Any]:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        config = self._config or _build_gmail_mcp_config()
        if not config:
            return []
        try:
            client = MultiServerMCPClient({"google-workspace": config})
            return list(await client.get_tools())
        except Exception as e:
            log.warning("Google Workspace MCP load failed: %s", e)
            return []

    async def _ensure_tools(self) -> list[Any]:
        if self._tools is None:
            self._tools = await self._load_tools()
        return self._tools

    async def search(self, query: str, limit: int = 25) -> list[dict[str, str]]:
        """Return message refs matching a Gmail query."""
        tools = await self._ensure_tools()
        tool = _find_tool(tools, _SEARCH_TOOL, _SEARCH_TERMS)
        if tool is None:
            log.info("No Gmail search tool available")
            return []
        try:
            result = await tool.ainvoke({"query": query, "user_google_email": self._account, "page_size": limit})
        except Exception as e:
            log.warning("Gmail search failed for %r: %s", query, e)
            return []

        text = _result_text(result)
        if _looks_like_error(text):
            log.warning("Gmail search error for %r: %s", query, text[:200])
            return []

        ids = _MSG_ID_RE.findall(text)
        threads = _THREAD_ID_RE.findall(text)
        refs: list[dict[str, str]] = []
        for i, mid in enumerate(ids[:limit]):
            refs.append({"message_id": mid, "thread_id": threads[i] if i < len(threads) else ""})
        return refs

    async def fetch(self, message_ids: Sequence[str]) -> list[EmailMessage]:
        """Fetch full text content for each message id."""
        tools = await self._ensure_tools()
        tool = _find_tool(tools, _READ_TOOL, _READ_TERMS)
        if tool is None:
            log.info("No Gmail read tool available")
            return []

        out: list[EmailMessage] = []
        for mid in message_ids:
            if not mid:
                continue
            try:
                result = await tool.ainvoke({"message_id": mid, "user_google_email": self._account})
            except Exception as e:
                log.warning("Gmail fetch failed for %s: %s", mid, e)
                continue
            text = _result_text(result)
            if text and not _looks_like_error(text):
                out.append(EmailMessage(message_id=mid, text=text[:_MAX_BODY_CHARS]))
        return out

    async def archive(self, message_id: str) -> bool:
        """Remove the INBOX label (Gmail Archive). False if unavailable/denied."""
        tools = await self._ensure_tools()
        tool = _find_tool(tools, _MODIFY_TOOL, _MODIFY_TERMS)
        if tool is None:
            log.warning(
                "No Gmail modify tool — cannot archive %s. OAuth grant likely lacks "
                "the gmail.modify scope; re-authorize Google to enable archiving.",
                message_id,
            )
            return False
        try:
            result = await tool.ainvoke(
                {
                    "message_id": message_id,
                    "user_google_email": self._account,
                    "remove_label_ids": ["INBOX"],
                }
            )
        except Exception as e:
            log.warning("Gmail archive failed for %s: %s", message_id, e)
            return False
        text = _result_text(result)
        if _looks_like_error(text):
            log.warning("Gmail archive error for %s: %s", message_id, text[:200])
            return False
        return True


def archiving_enabled() -> bool:
    """Whether processed emails should be archived (removed from the inbox).

    OFF by default as a safety measure: while the pipeline is being trusted, it
    records to Notion and reports but leaves every email in the inbox. Flip
    ``EMAIL_EXPENSES_ARCHIVE=true`` (e.g. on prod, no redeploy needed) once the
    recording is verified. Idempotency does not depend on this — the ledger, not
    the inbox, tracks what has been processed.
    """
    return os.environ.get("EMAIL_EXPENSES_ARCHIVE", "false").strip().lower() in {"1", "true", "yes", "on"}


def get_gmail_client() -> GmailClient | None:
    """Build the pipeline's Gmail client, or None if not configured."""
    account = os.environ.get("GMAIL_ACCOUNT", "").strip()
    if not account:
        log.info("GMAIL_ACCOUNT not set — email-expenses Gmail disabled")
        return None
    config = _build_gmail_mcp_config()
    if not config:
        log.info("Google OAuth not configured — email-expenses Gmail disabled")
        return None
    return GmailClient(account, config)
