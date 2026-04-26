"""Shared Telegram client references.

Two clients:
- bot client: set by bridge after login, used for sending messages
- userbot client: lazy-initialized from userbot.session, used for reading
  group history (bot API cannot iter_dialogs/iter_messages on groups)

Cron jobs that need to READ groups should use get_userbot().
Cron jobs that only SEND messages can use get_client() (bot).
"""

import logging
import os

from telethon import TelegramClient

from kronos.config import settings

log = logging.getLogger("kronos.telegram_client")

_client: TelegramClient | None = None
_userbot: TelegramClient | None = None


# ---------------------------------------------------------------------------
# Bot client (set by bridge)
# ---------------------------------------------------------------------------

def set_client(client: TelegramClient) -> None:
    global _client
    _client = client


def get_client() -> TelegramClient | None:
    return _client


# ---------------------------------------------------------------------------
# Userbot client (lazy init from session file)
# ---------------------------------------------------------------------------

async def get_userbot() -> TelegramClient | None:
    """Get or create userbot client for reading groups.

    Uses userbot.session file created by scripts/auth-userbot.py.
    Returns None if session doesn't exist or auth failed.
    """
    global _userbot

    if _userbot is not None:
        if _userbot.is_connected():
            return _userbot
        # Reconnect if disconnected
        try:
            await _userbot.connect()
            if await _userbot.is_user_authorized():
                return _userbot
        except Exception as e:
            log.warning("Userbot reconnect failed: %s", e)
            _userbot = None

    # Find session file
    session_name = os.environ.get("USERBOT_SESSION", "userbot")
    if not settings.tg_api_id or not settings.tg_api_hash:
        log.debug("TG_API_ID/HASH not set, userbot unavailable")
        return None

    try:
        client = TelegramClient(session_name, settings.tg_api_id, settings.tg_api_hash)
        await client.connect()

        if not await client.is_user_authorized():
            log.warning("Userbot session not authorized (run scripts/auth-userbot.py)")
            await client.disconnect()
            return None

        me = await client.get_me()
        log.info("Userbot connected: %s (@%s)", me.first_name, me.username)
        _userbot = client
        return _userbot

    except Exception as e:
        log.error("Failed to init userbot: %s", e)
        return None
