import pytest

from kronos.config import settings
from kronos.cron.expenses import gmail as gmail_mod
from kronos.cron.expenses.gmail import EmailMessage, GmailClient, get_gmail_client


class FakeTool:
    """MCP tool stub returning Workspace-MCP text-block results."""

    def __init__(self, name, response, description=""):
        self.name = name
        self.description = description or name
        self.response = response
        self.calls = []

    async def ainvoke(self, args):
        self.calls.append(args)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _text_block(text):
    return [{"type": "text", "text": text}]


def _client_with(tools):
    client = GmailClient("me@gmail.com", config={"stub": True})
    client._tools = tools  # bypass MCP load
    return client


@pytest.mark.asyncio
async def test_search_parses_message_refs():
    search = FakeTool(
        "search_gmail_messages",
        _text_block(
            "Found 2 messages matching 'from:grab':\n\n"
            "  1. Message ID: m1\n     Thread ID: t1\n\n"
            "  2. Message ID: m2\n     Thread ID: t2\n"
        ),
    )
    client = _client_with([search])

    refs = await client.search("from:grab", limit=25)

    assert [r["message_id"] for r in refs] == ["m1", "m2"]
    assert refs[0]["thread_id"] == "t1"
    assert search.calls[0]["query"] == "from:grab"
    assert search.calls[0]["user_google_email"] == "me@gmail.com"
    assert search.calls[0]["page_size"] == 25


@pytest.mark.asyncio
async def test_search_error_returns_empty():
    search = FakeTool(
        "search_gmail_messages",
        _text_block("2 validation errors for call[search_gmail_messages] query Missing required argument"),
    )
    client = _client_with([search])

    assert await client.search("from:grab") == []


@pytest.mark.asyncio
async def test_fetch_returns_email_text():
    read = FakeTool(
        "get_gmail_message_content",
        _text_block("Subject: Your Grab receipt\nFrom: Grab\nDate: 2026-07-05\n--- BODY ---\nPaid 41,500 IDR"),
    )
    client = _client_with([read])

    messages = await client.fetch(["m1"])

    assert len(messages) == 1
    assert isinstance(messages[0], EmailMessage)
    assert messages[0].message_id == "m1"
    assert "Your Grab receipt" in messages[0].text
    assert "Paid 41,500 IDR" in messages[0].text
    assert read.calls[0] == {"message_id": "m1", "user_google_email": "me@gmail.com"}


@pytest.mark.asyncio
async def test_fetch_skips_error_content():
    read = FakeTool("get_gmail_message_content", _text_block("Invalid arguments for tool 'get_gmail_message_content'"))
    client = _client_with([read])

    assert await client.fetch(["m1"]) == []


@pytest.mark.asyncio
async def test_archive_removes_inbox_label():
    modify = FakeTool("modify_gmail_message_labels", _text_block("Successfully modified message m1: removed [INBOX]"))
    client = _client_with([modify])

    ok = await client.archive("m1")

    assert ok is True
    assert modify.calls[0]["message_id"] == "m1"
    assert modify.calls[0]["remove_label_ids"] == ["INBOX"]
    assert modify.calls[0]["user_google_email"] == "me@gmail.com"


@pytest.mark.asyncio
async def test_archive_without_modify_tool_returns_false():
    # readonly OAuth grant: only search/read tools exist, no modify tool
    search = FakeTool("search_gmail_messages", _text_block("Found 0 messages"))
    client = _client_with([search])

    assert await client.archive("m1") is False


@pytest.mark.asyncio
async def test_archive_error_response_returns_false():
    modify = FakeTool("modify_gmail_message_labels", _text_block("authentication fail: insufficient permission"))
    client = _client_with([modify])

    assert await client.archive("m1") is False


def test_get_gmail_client_none_without_account(monkeypatch):
    monkeypatch.delenv("GMAIL_ACCOUNT", raising=False)
    assert get_gmail_client() is None


def test_get_gmail_client_none_without_oauth(monkeypatch):
    monkeypatch.setenv("GMAIL_ACCOUNT", "me@gmail.com")
    monkeypatch.setattr(settings, "google_oauth_client_id", "")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "")
    assert get_gmail_client() is None


def test_get_gmail_client_built_when_configured(monkeypatch):
    monkeypatch.setenv("GMAIL_ACCOUNT", "me@gmail.com")
    monkeypatch.setattr(settings, "google_oauth_client_id", "cid")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "secret")

    client = get_gmail_client()

    assert isinstance(client, GmailClient)
    assert client._account == "me@gmail.com"


def test_build_config_uses_gmail_tools(monkeypatch):
    monkeypatch.setattr(settings, "google_oauth_client_id", "cid")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "secret")
    monkeypatch.delenv("EXPENSES_GMAIL_MCP_TOOLS", raising=False)

    config = gmail_mod._build_gmail_mcp_config()

    assert config is not None
    assert "--tools" in config["args"]
    assert "gmail" in config["args"]
    assert config["env"]["GOOGLE_OAUTH_CLIENT_ID"] == "cid"
