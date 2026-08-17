"""Noticing that an MCP server stopped handing over its tools.

Two of them were dead for months and nothing said so. The loading is resilient
by design — a server that will not start is skipped so the rest keep working —
which is right, and which is exactly why it went unseen: the agents came up with
102 tools instead of 113, nothing errored, and the finance agent went on
answering market questions from news search alone.

So what is tested here is the reporting, not the loading: that a fault is
distinguished from a deliberate absence, that a server which vanishes from the
config is not silently forgotten, and that "started" is not mistaken for
"working".
"""

import pytest

from kronos.config import settings
from kronos.health import STATUS_BROKEN, STATUS_OFF, STATUS_OK
from kronos.tools import manager
from kronos.tools.mcp_servers import KNOWN_SERVERS, build_mcp_config


@pytest.fixture
def servers(monkeypatch):
    """Pretend a given set of servers is configured."""

    def _configure(*names):
        monkeypatch.setattr(manager, "build_mcp_config", lambda: {n: {"transport": "stdio"} for n in names})

    return _configure


@pytest.fixture
def loader(monkeypatch):
    """Decide what each server answers when asked for its tools."""

    def _respond(answers):
        calls = []

        async def _load(name, config, timeout=None, capture_stderr=False):
            calls.append((name, timeout))
            return answers.get(name, ([FakeTool(name)], ""))

        monkeypatch.setattr(manager, "_load_server_tools", _load)
        return calls

    return _respond


class FakeTool:
    """Enough of a tool for the manager to log it."""

    def __init__(self, name="t"):
        self.name = name
        self.metadata = None


def by_name(checks):
    return {c.name: c for c in checks}


# --- the list the probe diffs against -----------------------------------------


def test_known_servers_matches_what_the_builder_can_produce(monkeypatch, tmp_path):
    """A server added without updating the list would never be probed.

    KNOWN_SERVERS is what makes a *vanished* server visible, so it drifting out
    of step with the builder would reopen the hole quietly.
    """
    monkeypatch.setattr(settings, "brave_api_key", "x")
    monkeypatch.setattr(settings, "exa_api_key", "x")
    monkeypatch.setattr(settings, "notion_api_key", "x")
    monkeypatch.setattr(settings, "google_oauth_client_id", "x")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "x")
    monkeypatch.setattr(settings, "agent_name", "kronos")
    monkeypatch.setattr(settings, "workspace_path", str(tmp_path))
    monkeypatch.setenv("GOOGLE_WORKSPACE_MCP_AGENT", "kronos")

    assert set(build_mcp_config()) == set(KNOWN_SERVERS)


def test_the_probe_reports_in_a_stable_order(servers, loader):
    """Otherwise the message reshuffles itself between identical runs."""
    servers(*KNOWN_SERVERS)
    loader({})

    import asyncio

    checks = asyncio.run(manager.check_mcp_health())

    assert [c.name for c in checks] == list(KNOWN_SERVERS)


# --- a fault is not the same as a deliberate absence --------------------------


@pytest.mark.asyncio
async def test_an_unconfigured_server_is_off_not_broken(servers, loader):
    """Most deployments configure a handful of these; alerting would be noise."""
    servers("fetch")
    loader({})

    checks = by_name(await manager.check_mcp_health())

    assert checks["fetch"].status == STATUS_OK
    assert checks["notion"].status == STATUS_OFF
    assert "not configured" in checks["notion"].detail


@pytest.mark.asyncio
async def test_a_server_that_vanished_is_still_named(servers, loader):
    """A key dropped from .env makes a server disappear from the config entirely.

    Left out of the report it would be forgotten rather than missed — the shared
    reporter neither compares nor remembers a check it is not given.
    """
    servers("fetch")
    loader({})

    checks = by_name(await manager.check_mcp_health())

    assert set(checks) == set(KNOWN_SERVERS)


@pytest.mark.asyncio
async def test_a_failing_server_reports_why(servers, loader):
    """The reason existed all along — in a journal traceback nobody read."""
    servers("yahoo-finance")
    loader({"yahoo-finance": ([], "AttributeError: 'Server' object has no attribute 'list_tools'")})

    checks = by_name(await manager.check_mcp_health())

    assert checks["yahoo-finance"].status == STATUS_BROKEN
    assert "list_tools" in checks["yahoo-finance"].detail


@pytest.mark.asyncio
async def test_starting_is_not_the_same_as_working(servers, loader):
    """A server that comes up clean and offers nothing contributes nothing.

    Every check that stops at "did the process start" calls this healthy.
    """
    servers("reddit")
    loader({"reddit": ([], "")})

    checks = by_name(await manager.check_mcp_health())

    assert checks["reddit"].status == STATUS_BROKEN
    assert "no tools" in checks["reddit"].detail


@pytest.mark.asyncio
async def test_a_working_server_reports_how_many_tools(servers, loader):
    """ "ok" with no number is what 9-of-11 looked like for months."""
    servers("notion")
    loader({"notion": ([FakeTool()] * 24, "")})

    checks = by_name(await manager.check_mcp_health())

    assert checks["notion"].status == STATUS_OK
    assert "24 tools" in checks["notion"].detail


# --- the probe must not hang ---------------------------------------------------


@pytest.mark.asyncio
async def test_every_server_is_probed_under_a_timeout(servers, loader):
    """One server that never answers would otherwise stall the whole job."""
    servers("fetch", "notion")
    calls = loader({})

    await manager.check_mcp_health()

    assert [timeout for _, timeout in calls] == [manager.PROBE_TIMEOUT_SECONDS] * 2


@pytest.mark.asyncio
async def test_a_timeout_is_reported_as_broken(servers, loader):
    servers("fetch")
    loader({"fetch": ([], "no answer within 60s")})

    checks = by_name(await manager.check_mcp_health())

    assert checks["fetch"].status == STATUS_BROKEN
    assert "no answer" in checks["fetch"].detail


# --- saying why, not just that ------------------------------------------------


def test_a_wrapper_exception_is_unwrapped_to_the_real_cause():
    """anyio delivers a dead server as a TaskGroup ExceptionGroup.

    "unhandled errors in a TaskGroup (1 sub-exception)" is true and useless, and
    it is what this reported until the cause was dug out — the same cause that
    only ever appeared in a journal traceback nobody read.
    """
    inner = AttributeError("'Server' object has no attribute 'list_tools'")
    wrapped = BaseExceptionGroup("outer", [BaseExceptionGroup("inner", [inner])])

    assert manager.describe_failure(wrapped) == "AttributeError: 'Server' object has no attribute 'list_tools'"


def test_several_distinct_causes_are_all_named_once():
    group = BaseExceptionGroup("outer", [ValueError("a"), ValueError("a"), KeyError("b")])

    described = manager.describe_failure(group)

    assert described.count("ValueError: a") == 1
    assert "KeyError: 'b'" in described


def test_a_plain_exception_is_left_alone():
    assert manager.describe_failure(RuntimeError("boom")) == "RuntimeError: boom"


def test_a_dying_server_s_last_words_reach_the_report():
    """The protocol only knows "Connection closed"; the reason went to stderr.

    Silencing that stream to clean up the table would have thrown away the one
    thing worth reading, which is why it is captured rather than discarded.
    """
    said = "Traceback (most recent call last):\n  File x\nAttributeError: no attribute 'list_tools'\n"

    detail = manager._with_last_words("McpError: Connection closed", said)

    assert "Connection closed" in detail
    assert "list_tools" in detail


def test_last_words_are_stripped_of_terminal_colours():
    """One server writes green ANSI codes; they are noise in a chat message."""
    detail = manager._with_last_words("failed", "\x1b[32mProcessing request\x1b[0m\n")

    assert "\x1b" not in detail
    assert "Processing request" in detail


def test_a_server_that_died_silently_gets_nothing_appended():
    """No last words is not a reason to invent a dangling "server said:"."""
    assert manager._with_last_words("McpError: Connection closed", "   \n\n") == "McpError: Connection closed"


@pytest.mark.asyncio
async def test_capture_is_off_unless_asked_for(monkeypatch):
    """fd 2 belongs to the whole process.

    Capturing it inside a running agent would swallow every other task's logging
    for forty seconds a day, so the long-lived caller does not opt in.
    """
    seen = []

    async def _load(name, config, timeout=None, capture_stderr=False):
        seen.append(capture_stderr)
        return [FakeTool()], ""

    monkeypatch.setattr(manager, "build_mcp_config", lambda: {"fetch": {}})
    monkeypatch.setattr(manager, "_load_server_tools", _load)

    await manager.check_mcp_health()
    async with manager.managed_mcp_tools():
        pass

    assert seen == [False, False]


# --- startup keeps its own behaviour -------------------------------------------


@pytest.mark.asyncio
async def test_startup_does_not_impose_the_probe_s_timeout(monkeypatch):
    """Bounding startup is a different decision from bounding a daily check.

    Giving up on a slow-but-working server at boot would cost tools for the whole
    session; the probe only costs one line in a report.
    """
    seen = []

    async def _load(name, config, timeout=None, capture_stderr=False):
        seen.append(timeout)
        return [FakeTool()], ""

    monkeypatch.setattr(manager, "build_mcp_config", lambda: {"fetch": {}})
    monkeypatch.setattr(manager, "_load_server_tools", _load)

    async with manager.managed_mcp_tools() as tools:
        assert len(tools) == 1

    assert seen == [None]


@pytest.mark.asyncio
async def test_a_failure_report_never_carries_the_server_s_credentials(monkeypatch):
    """Server configs hold API keys in `env`, and this report goes to Telegram.

    The detail comes from the exception, so a library that helpfully includes the
    config it was handed would put a key in a chat message.
    """
    secret = "brave-key-do-not-leak"
    monkeypatch.setattr(
        manager,
        "build_mcp_config",
        lambda: {"brave-search": {"transport": "stdio", "env": {"BRAVE_API_KEY": secret}}},
    )

    async def _load(name, config, timeout=None, capture_stderr=False):
        return [], f"RuntimeError: could not start with {config}"

    monkeypatch.setattr(manager, "_load_server_tools", _load)

    checks = await manager.check_mcp_health()

    assert all(secret not in c.detail for c in checks), "a credential reached the report"


@pytest.mark.asyncio
async def test_a_credential_embedded_in_a_larger_value_is_redacted_too(monkeypatch):
    """Notion's env entry is a JSON header string with the token inside it.

    Redacting only whole env values would miss the token quoted on its own —
    which is the form an error message is most likely to carry.
    """
    token = "ntn_A1B2C3D4E5F6G7H8I9J0"
    header = '{"Authorization":"Bearer ' + token + '","Notion-Version":"2022-06-28"}'
    monkeypatch.setattr(
        manager,
        "build_mcp_config",
        lambda: {"notion": {"transport": "stdio", "env": {"OPENAPI_MCP_HEADERS": header}}},
    )

    async def _load(name, config, timeout=None, capture_stderr=False):
        return [], f"RuntimeError: rejected token {token}"

    monkeypatch.setattr(manager, "_load_server_tools", _load)

    notion = by_name(await manager.check_mcp_health())["notion"]

    assert token not in notion.detail
    assert "***" in notion.detail
