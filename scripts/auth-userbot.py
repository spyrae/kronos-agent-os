#!/usr/bin/env python3
"""One-time Telethon userbot authorization.

Creates <agent_name>.session file for the specified agent.
Run interactively — will ask for phone number and Telegram code.

Usage:
    cd /opt/kaos/app

    # Auth specific agent (reads from its .env.<agent>):
    AGENT_NAME=kaos-worker TG_API_ID=... TG_API_HASH=... .venv/bin/python scripts/auth-userbot.py

    # Or with default agent from .env:
    .venv/bin/python scripts/auth-userbot.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["TG_BOT_TOKEN"] = ""

from telethon import TelegramClient
from kronos.config import settings


async def main():
    session_name = f"{settings.agent_name}.session"
    print(f"Authorizing agent: {settings.agent_name}")
    print(f"Session file: {session_name}")
    print(f"API ID: {settings.tg_api_id}")

    client = TelegramClient(session_name, settings.tg_api_id, settings.tg_api_hash)
    await client.start()
    me = await client.get_me()
    print(f"OK: {me.first_name} (@{me.username}, id={me.id})")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
