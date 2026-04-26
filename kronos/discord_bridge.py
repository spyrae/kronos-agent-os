"""Discord bridge — second communication channel alongside Telegram.

Uses the same KronosAgent and Mem0 memory store.
Thread isolation: thread_id = f"discord:{channel_id}:{thread_id}"

Requires: discord.py>=2.0 (optional dependency)
"""

import asyncio
import logging
import time

from kronos.audit import log_request
from kronos.config import settings
from kronos.graph import KronosAgent
from kronos.router import classify_tier

log = logging.getLogger("kronos.discord_bridge")

_agent: KronosAgent | None = None


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
            user_id=str(user_id),
            session_id=graph_thread,
        )
    except Exception as e:
        log.error("Agent error: %s", e)
        return f"Произошла ошибка: {type(e).__name__}: {str(e)[:200]}"

    if not reply:
        reply = "Не удалось получить ответ."

    # Audit
    duration_ms = int(time.monotonic() * 1000) - start_ms
    tier = classify_tier(message).value
    log_request(
        user_id=str(user_id),
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

    allowed_guilds = set()
    if settings.discord_allowed_guilds:
        allowed_guilds = {
            int(gid.strip())
            for gid in settings.discord_allowed_guilds.split(",")
            if gid.strip()
        }

    @client.event
    async def on_ready():
        log.info("Discord bot logged in as %s (guilds: %d)", client.user, len(client.guilds))

    @client.event
    async def on_message(message: discord.Message):
        # Ignore own messages
        if message.author == client.user:
            return

        # Ignore bots
        if message.author.bot:
            return

        # Guild filter
        if allowed_guilds and message.guild and message.guild.id not in allowed_guilds:
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

        channel_label = f"#{message.channel}" if hasattr(message.channel, 'name') else "DM"
        log.info("[Discord %s] %s: %s", channel_label, message.author, text[:100])

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
            chunks = [reply[i:i + 2000] for i in range(0, len(reply), 2000)]
            for chunk in chunks:
                await message.channel.send(chunk)
        else:
            await message.reply(reply)

        log.info("[Discord] Replied to %s: %s", message.author, reply[:100])

    log.info("Starting Discord bot...")
    await client.start(settings.discord_bot_token)
