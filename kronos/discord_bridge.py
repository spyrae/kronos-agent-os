"""Discord bridge — second communication channel alongside Telegram.

Uses the same KronosAgent and Mem0 memory store.
Thread isolation: thread_id = f"discord:{channel_id}:{thread_id}"

Access is an explicit user allowlist (``DISCORD_ALLOWED_USERS``). The agent
carries the owner's memory, accounts and tools, so a bot token alone answers
nobody: not a DM from a stranger, not a mention in a server the bot was
invited to. ``DISCORD_ALLOWED_GUILDS`` only narrows where allowed users are heard.

A Discord user is ``discord:<id>`` everywhere downstream. Telegram and Discord
both hand out bare numbers; without the prefix one platform's user could match
the other's memory.

Requires: discord.py>=2.0 (optional dependency)
"""

import asyncio
import logging
import time

from kronos.audit import log_request
from kronos.config import settings
from kronos.graph import KronosAgent
from kronos.router import classify_tier
from kronos.security.output_validator import validate_output

log = logging.getLogger("kronos.discord_bridge")

_agent: KronosAgent | None = None


def _id_set(raw: str) -> tuple[set[int], list[str]]:
    """Parse a comma-separated id list; return the valid ids and the rejected tokens."""
    valid: set[int] = set()
    invalid: list[str] = []
    for token in raw.split(","):
        token = token.strip()
        if not token or token.startswith("#"):
            continue
        if token.isdecimal():
            valid.add(int(token))
        else:
            invalid.append(token)
    return valid, invalid


def allowed_users() -> set[int]:
    return _id_set(settings.discord_allowed_users)[0]


def allowed_guilds() -> set[int]:
    return _id_set(settings.discord_allowed_guilds)[0]


def discord_user_key(user_id: int) -> str:
    """Identity used for memory, budgets and audit; never a bare platform number."""
    return f"discord:{int(user_id)}"


def may_talk(*, author_id: int, author_is_bot: bool, guild_id: int | None) -> bool:
    """Whether a message may reach the agent at all.

    The sender must be on the user allowlist. In a server, the server must also
    be allowed when a guild list is configured. A malformed guild list refuses
    server messages rather than silently meaning "any server".
    """
    if author_is_bot or author_id not in allowed_users():
        return False
    if guild_id is None:
        return True
    guilds, invalid = _id_set(settings.discord_allowed_guilds)
    if invalid:
        return False
    return not guilds or guild_id in guilds


async def _ask_agent(
    message: str,
    channel_id: int,
    user_id: int,
    thread_id: int | None = None,
) -> str:
    """Send message to KronosAgent and return response text."""
    # Discord thread isolation
    if thread_id:
        graph_thread = f"discord:{channel_id}:{thread_id}"
    else:
        graph_thread = f"discord:{channel_id}"

    start_ms = int(time.monotonic() * 1000)

    try:
        reply = await _agent.ainvoke(
            message=message,
            thread_id=graph_thread,
            user_id=discord_user_key(user_id),
            session_id=graph_thread,
        )
    except Exception as e:
        # The exception text can carry paths, URLs or provider responses.
        log.error("Agent error: %s", type(e).__name__)
        return "Произошла ошибка при обработке запроса. Попробуй ещё раз."

    if not reply:
        reply = "Не удалось получить ответ."

    # Same outbound gate as Telegram: redact secrets before anything is sent.
    validation = validate_output(reply)
    if not validation.is_clean:
        reply = validation.redacted_text

    # Audit
    duration_ms = int(time.monotonic() * 1000) - start_ms
    tier = classify_tier(message).value
    log_request(
        user_id=discord_user_key(user_id),
        session_id=graph_thread,
        tier=tier,
        input_text=message,
        output_text=reply,
        duration_ms=duration_ms,
    )

    return reply


async def run_discord(agent: KronosAgent) -> None:
    """Start Discord bot. Runs alongside Telegram bridge in asyncio.gather()."""
    global _agent
    _agent = agent

    if not settings.discord_bot_token:
        log.info("Discord disabled: DISCORD_BOT_TOKEN not set")
        # Return a never-completing future so asyncio.gather doesn't exit
        await asyncio.Event().wait()
        return

    try:
        import discord
    except ImportError:
        log.warning("Discord disabled: discord.py not installed (pip install discord.py)")
        await asyncio.Event().wait()
        return

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    users, bad_users = _id_set(settings.discord_allowed_users)
    _, bad_guilds = _id_set(settings.discord_allowed_guilds)
    if bad_users or bad_guilds:
        log.error(
            "Discord allowlists contain %d invalid id(s); those entries grant nothing", len(bad_users) + len(bad_guilds)
        )
    if not users:
        log.warning("Discord is connected but answers nobody: DISCORD_ALLOWED_USERS is empty")

    @client.event
    async def on_ready():
        log.info("Discord bot logged in as %s (guilds: %d)", client.user, len(client.guilds))

    @client.event
    async def on_message(message: discord.Message):
        # Ignore own messages
        if message.author == client.user:
            return

        # Owner allowlist first: strangers, bots and unlisted servers get no
        # reply at all, so the bot does not even confirm that it is listening.
        if not may_talk(
            author_id=message.author.id,
            author_is_bot=bool(message.author.bot),
            guild_id=message.guild.id if message.guild else None,
        ):
            return

        # Only respond to mentions or DMs
        is_dm = isinstance(message.channel, discord.DMChannel)
        is_mentioned = client.user in message.mentions

        if not is_dm and not is_mentioned:
            return

        # Strip mention from text
        text = message.content
        if client.user:
            text = text.replace(f"<@{client.user.id}>", "").strip()

        if not text:
            return

        # Thread context
        thread_id = None
        if isinstance(message.channel, discord.Thread):
            thread_id = message.channel.id

        channel_label = "channel" if hasattr(message.channel, "name") else "DM"
        log.info("[Discord %s] message from allowed user %s (%d chars)", channel_label, message.author.id, len(text))

        # Show typing while processing
        async with message.channel.typing():
            reply = await _ask_agent(
                text,
                channel_id=message.channel.id,
                user_id=message.author.id,
                thread_id=thread_id,
            )

        # Discord message limit is 2000 chars
        if len(reply) > 2000:
            chunks = [reply[i : i + 2000] for i in range(0, len(reply), 2000)]
            for chunk in chunks:
                await message.channel.send(chunk)
        else:
            await message.reply(reply)

        log.info("[Discord] Replied to %s (%d chars)", message.author.id, len(reply))

    log.info("Starting Discord bot...")
    await client.start(settings.discord_bot_token)
