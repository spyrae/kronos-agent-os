"""A01: Discord answers only allowlisted owners, and never as a Telegram identity."""

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import kronos.discord_bridge as bridge
from kronos.config import settings

OWNER, STRANGER, GUILD, OTHER_GUILD = 111, 999, 5000, 6000


@pytest.fixture(autouse=True)
def allowlists(monkeypatch):
    monkeypatch.setattr(settings, "discord_allowed_users", str(OWNER))
    monkeypatch.setattr(settings, "discord_allowed_guilds", "")


def _may(author=OWNER, bot=False, guild=None):
    return bridge.may_talk(author_id=author, author_is_bot=bot, guild_id=guild)


def test_an_empty_user_allowlist_means_nobody(monkeypatch):
    monkeypatch.setattr(settings, "discord_allowed_users", "")
    assert not _may() and not _may(guild=GUILD)
    monkeypatch.setattr(settings, "discord_allowed_guilds", str(GUILD))
    assert not _may(guild=GUILD), "an allowed server does not make its members owners"


def test_only_allowlisted_users_are_heard_in_dms_and_in_servers():
    assert _may() and _may(guild=GUILD)
    assert not _may(author=STRANGER) and not _may(author=STRANGER, guild=GUILD)
    assert not _may(bot=True), "an allowlisted id that is a bot is still a bot"


def test_a_guild_list_narrows_servers_but_never_blocks_the_owners_dm(monkeypatch):
    monkeypatch.setattr(settings, "discord_allowed_guilds", str(GUILD))
    assert _may(guild=GUILD) and _may(guild=None)
    assert not _may(guild=OTHER_GUILD)


@pytest.mark.parametrize("raw", ["abc", "5000,abc", "5000;6000", "-5000"])
def test_a_malformed_guild_list_does_not_mean_every_server(monkeypatch, raw):
    monkeypatch.setattr(settings, "discord_allowed_guilds", raw)
    assert not _may(guild=GUILD) and not _may(guild=OTHER_GUILD)
    assert _may(guild=None)


@pytest.mark.parametrize("raw", ["abc", "*", "all", "111x", " , ", "# 111"])
def test_a_malformed_user_list_grants_nothing(monkeypatch, raw):
    monkeypatch.setattr(settings, "discord_allowed_users", raw)
    assert not _may() and not _may(author=STRANGER)


def test_ids_are_matched_exactly_not_by_prefix(monkeypatch):
    monkeypatch.setattr(settings, "discord_allowed_users", "1110, 21110")
    assert not _may(author=111) and _may(author=1110)


def test_a_discord_user_never_shares_an_identity_with_a_telegram_user():
    assert bridge.discord_user_key(OWNER) == "discord:111"
    assert bridge.discord_user_key(OWNER) != str(OWNER)


async def test_the_agent_receives_the_namespaced_identity_and_redacted_output(monkeypatch):
    agent = SimpleNamespace(ainvoke=AsyncMock(return_value="token sk-abcdefghijklmnop123456 here"))
    monkeypatch.setattr(bridge, "_agent", agent)
    monkeypatch.setattr(bridge, "log_request", lambda **kwargs: None)

    reply = await bridge._ask_agent("hi", channel_id=7, user_id=OWNER, thread_id=9)

    call = agent.ainvoke.await_args.kwargs
    assert call["user_id"] == "discord:111" and call["thread_id"] == "discord:7:9"
    assert "sk-abcdefghijklmnop123456" not in reply


async def test_an_agent_error_does_not_leak_its_text_to_the_chat(monkeypatch):
    agent = SimpleNamespace(ainvoke=AsyncMock(side_effect=RuntimeError("/opt/secret/path token=abc123")))
    monkeypatch.setattr(bridge, "_agent", agent)

    reply = await bridge._ask_agent("hi", channel_id=7, user_id=OWNER)

    assert "secret" not in reply and "RuntimeError" not in reply and "abc123" not in reply


# --- the real handler, driven through a stand-in for discord.py ---------------


class _FakeClient:
    def __init__(self, intents=None):
        self.user = SimpleNamespace(id=42)
        self.guilds = []
        self.handlers = {}

    def event(self, fn):
        self.handlers[fn.__name__] = fn
        return fn

    async def start(self, token):
        return None


class _Typing:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def _fake_discord(monkeypatch):
    module = types.ModuleType("discord")
    module.Client = _FakeClient
    module.Intents = SimpleNamespace(default=lambda: SimpleNamespace(message_content=False))
    module.DMChannel = type("DMChannel", (), {})
    module.Thread = type("Thread", (), {})
    module.Message = object
    monkeypatch.setitem(sys.modules, "discord", module)
    return module


def _message(discord, *, author=OWNER, guild=None, dm=True, mentioned=False, bot=False, text="hello"):
    channel = (discord.DMChannel if dm else type("TextChannel", (), {"name": "general"}))()
    channel.id = 7
    channel.typing = lambda: _Typing()
    channel.send = AsyncMock()
    return SimpleNamespace(
        author=SimpleNamespace(id=author, bot=bot),
        guild=SimpleNamespace(id=guild) if guild else None,
        channel=channel,
        mentions=[],
        content=text,
        reply=AsyncMock(),
        _mentioned=mentioned,
    )


async def _handler(monkeypatch):
    discord = _fake_discord(monkeypatch)
    monkeypatch.setattr(settings, "discord_bot_token", "token")
    monkeypatch.setattr(bridge, "log_request", lambda **kwargs: None)
    agent = SimpleNamespace(ainvoke=AsyncMock(return_value="answer"))
    created = {}
    original = discord.Client

    def capture(intents=None):
        created["client"] = original(intents)
        return created["client"]

    discord.Client = capture
    await bridge.run_discord(agent)
    client = created["client"]
    return discord, client, client.handlers["on_message"], agent


async def test_a_stranger_gets_no_reply_and_never_reaches_the_agent(monkeypatch):
    discord, client, on_message, agent = await _handler(monkeypatch)

    for message in (
        _message(discord, author=STRANGER),
        _message(discord, author=STRANGER, guild=GUILD, dm=False),
        _message(discord, author=OWNER, bot=True),
    ):
        if not isinstance(message.channel, discord.DMChannel):
            message.mentions = [client.user]
        await on_message(message)
        message.reply.assert_not_awaited()
        message.channel.send.assert_not_awaited()

    agent.ainvoke.assert_not_awaited()


async def test_the_owner_is_answered_in_a_dm_and_when_mentioned(monkeypatch):
    discord, client, on_message, agent = await _handler(monkeypatch)

    dm = _message(discord)
    await on_message(dm)
    dm.reply.assert_awaited_once_with("answer")

    mention = _message(discord, guild=GUILD, dm=False, text="<@42> status?")
    mention.mentions = [client.user]
    await on_message(mention)
    mention.reply.assert_awaited_once_with("answer")
    assert agent.ainvoke.await_args.kwargs["message"] == "status?"

    unaddressed = _message(discord, guild=GUILD, dm=False, text="chatting")
    await on_message(unaddressed)
    unaddressed.reply.assert_not_awaited()
    assert agent.ainvoke.await_count == 2


async def test_the_owner_is_ignored_in_a_server_outside_the_guild_list(monkeypatch):
    monkeypatch.setattr(settings, "discord_allowed_guilds", str(GUILD))
    discord, client, on_message, agent = await _handler(monkeypatch)

    message = _message(discord, guild=OTHER_GUILD, dm=False)
    message.mentions = [client.user]
    await on_message(message)

    message.reply.assert_not_awaited()
    agent.ainvoke.assert_not_awaited()
