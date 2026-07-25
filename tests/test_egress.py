"""Policy-driven egress control (moat phase 9.4)."""

import pytest
import yaml

from kronos.policy import ENV_POLICY_FILE, reset_policy
from kronos.security.egress import (
    EgressBlockedError,
    check_command,
    check_url,
    force_allowlist,
    host_allowed,
)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv(ENV_POLICY_FILE, raising=False)
    force_allowlist(False)
    reset_policy()
    yield
    force_allowlist(False)
    reset_policy()


def _policy(tmp_path, monkeypatch, egress: dict):
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump({"egress": egress}, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv(ENV_POLICY_FILE, str(path))
    reset_policy()


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("api.telegram.org", True),
        ("API.Telegram.ORG", True),  # case-insensitive
        ("api.telegram.org.", True),  # trailing dot
        ("evil.org", False),
        ("raw.githubusercontent.com", True),  # via *.githubusercontent.com
        ("githubusercontent.com", True),  # wildcard also matches the bare domain
        ("notgithubusercontent.com", False),  # not a subdomain
        ("", False),
    ],
)
def test_host_matching(host, allowed):
    domains = ["api.telegram.org", "*.githubusercontent.com"]

    assert host_allowed(host, domains) is allowed


def test_open_mode_allows_everything(tmp_path, monkeypatch):
    _policy(tmp_path, monkeypatch, {"mode": "open"})

    check_url("https://anything.example.invalid/path")  # no exception


def test_allowlist_blocks_unlisted_host(tmp_path, monkeypatch):
    _policy(tmp_path, monkeypatch, {"mode": "allowlist", "dry_run": False, "domains": ["api.telegram.org"]})

    check_url("https://api.telegram.org/bot/sendMessage")
    with pytest.raises(EgressBlockedError, match="evil.example.invalid is not in the allowlist"):
        check_url("https://evil.example.invalid/x", tool="browser_navigate")


def test_dry_run_reports_without_blocking(tmp_path, monkeypatch, caplog):
    """The rollout path: watch a day of traffic before enforcing."""
    _policy(tmp_path, monkeypatch, {"mode": "allowlist", "dry_run": True, "domains": []})

    check_url("https://unlisted.example.invalid/x", tool="fetch")

    assert "would block unlisted.example.invalid" in caplog.text


def test_localhost_is_always_reachable(tmp_path, monkeypatch):
    """A local Ollama or dashboard is not egress in any meaningful sense."""
    _policy(tmp_path, monkeypatch, {"mode": "allowlist", "dry_run": False, "domains": []})

    check_url("http://localhost:11434/v1/chat")
    check_url("http://127.0.0.1:8788/health")
    check_url("http://192.168.1.10:3000/")


def test_url_without_host_is_refused(tmp_path, monkeypatch):
    _policy(tmp_path, monkeypatch, {"mode": "allowlist", "dry_run": False, "domains": []})

    with pytest.raises(EgressBlockedError, match="cannot determine host"):
        check_url("not-a-url")


def test_demo_mode_forces_enforcement(tmp_path, monkeypatch):
    """Demo forces allowlist even when the policy says open with dry-run."""
    _policy(tmp_path, monkeypatch, {"mode": "open", "dry_run": True, "domains": []})
    force_allowlist(True)

    with pytest.raises(EgressBlockedError):
        check_url("https://example.invalid/x")


def test_mcp_command_allowlist(tmp_path, monkeypatch):
    _policy(
        tmp_path,
        monkeypatch,
        {"mode": "allowlist", "dry_run": False, "allowed_commands": ["npx", "/usr/bin/uvx"]},
    )

    check_command("npx", server="brave-search")
    check_command("/opt/homebrew/bin/uvx", server="fetch")  # matched by basename
    with pytest.raises(EgressBlockedError, match="'curl' is not in allowed_commands"):
        check_command("curl", server="sneaky")


def test_empty_command_list_means_unrestricted(tmp_path, monkeypatch):
    _policy(tmp_path, monkeypatch, {"mode": "allowlist", "dry_run": False, "allowed_commands": []})

    check_command("anything", server="x")  # no exception


@pytest.mark.asyncio
async def test_gateway_skips_servers_with_blocked_commands(tmp_path, monkeypatch):
    """One unlisted command must not take the whole agent down."""
    from langchain_core.tools import tool

    from kronos.tools import gateway as gateway_module

    _policy(
        tmp_path,
        monkeypatch,
        {"mode": "allowlist", "dry_run": False, "allowed_commands": ["npx"]},
    )

    @tool
    def allowed_tool() -> str:
        """From the permitted server."""
        return "ok"

    seen_configs: dict = {}

    class FakeClient:
        def __init__(self, config):
            seen_configs.update(config)

        async def get_tools(self):
            return [allowed_tool]

    monkeypatch.setattr(gateway_module, "MultiServerMCPClient", FakeClient)
    monkeypatch.setattr(
        gateway_module,
        "build_mcp_config",
        lambda: {
            "brave-search": {"command": "npx", "args": []},
            "sneaky": {"command": "curl", "args": []},
        },
    )

    tools = await gateway_module.MCPGateway().start()

    assert list(seen_configs) == ["brave-search"]
    assert len(tools) == 1


def test_browser_navigate_reports_a_policy_block(tmp_path, monkeypatch):
    """Navigation returns a message rather than raising into the model loop."""
    import asyncio

    from kronos.tools.browser import engine

    _policy(tmp_path, monkeypatch, {"mode": "allowlist", "dry_run": False, "domains": ["docs.example.com"]})

    result = asyncio.run(engine.navigate("https://evil.example.invalid/page"))

    assert "Navigation blocked" in result
    assert "not in the allowlist" in result


def test_skill_import_is_subject_to_the_allowlist(tmp_path, monkeypatch):
    """A skill is instructions the agent will follow, so its source is gated."""
    from kronos.skills import hub

    _policy(tmp_path, monkeypatch, {"mode": "allowlist", "dry_run": False, "domains": ["raw.githubusercontent.com"]})

    with pytest.raises(EgressBlockedError):
        hub._fetch_url("https://evil.example.invalid/SKILL.md")


def test_search_libraries_respect_the_allowlist(tmp_path, monkeypatch):
    """brave/exa are the funnel for agent tools AND cron pipelines alike."""
    from kronos.config import settings
    from kronos.tools import brave, exa

    _policy(tmp_path, monkeypatch, {"mode": "allowlist", "dry_run": False, "domains": ["api.exa.ai"]})
    monkeypatch.setattr(settings, "brave_api_key", "test-key")
    monkeypatch.setattr(settings, "exa_api_key", "test-key")
    monkeypatch.setattr(brave, "_brave_unavailable_until", 0.0, raising=False)

    # Brave's host is not listed → blocked before any request is made, and the
    # block is not swallowed by the Exa fallback.
    with pytest.raises(EgressBlockedError, match="api.search.brave.com"):
        brave.search("kaos", count=1)

    # Exa's host IS listed, so the allowlist lets the request start. exa.search
    # swallows transport errors by design (it is a fallback path), so the check is
    # "did we get as far as the socket", not "did it raise".
    attempts: list = []

    def fake_urlopen(*args, **kwargs):
        attempts.append(args)
        raise RuntimeError("network disabled in test")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    assert exa.search("kaos", count=1) == []
    assert attempts, "an allowlisted host should have been reached"


def test_telegram_channel_fetch_respects_the_allowlist(tmp_path, monkeypatch):
    import asyncio

    from kronos.tools import telegram_channels

    _policy(tmp_path, monkeypatch, {"mode": "allowlist", "dry_run": False, "domains": ["example.com"]})

    with pytest.raises(EgressBlockedError, match="t.me"):
        asyncio.run(telegram_channels.fetch_posts("@somechannel", limit=1))
