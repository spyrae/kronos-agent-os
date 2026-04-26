#!/usr/bin/env python3
"""List all Telegram groups/supergroups the user is a member of.

Run on VPS where Telethon session is active:
    cd /opt/kronos-ii/app && python scripts/list-groups.py

Outputs a markdown table ready to paste into GROUPS.md.
"""

import asyncio
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon import TelegramClient
from telethon.tl.types import Channel, Chat

from kronos.config import settings


async def main():
    session_file = os.environ.get("SESSION_FILE", "userbot")
    client = TelegramClient(session_file, settings.tg_api_id, settings.tg_api_hash)
    await client.start()

    me = await client.get_me()
    print(f"Logged in as: {me.first_name} (@{me.username})\n")

    groups = []

    async for dialog in client.iter_dialogs():
        entity = dialog.entity

        # Groups, supergroups, and broadcast channels
        if isinstance(entity, Channel):
            is_channel = not entity.megagroup  # broadcast channel
            is_group = entity.megagroup  # supergroup
        elif isinstance(entity, Chat):
            is_channel = False
            is_group = True
        else:
            continue  # skip users/DMs

        kind = "channel" if is_channel else "group"

        username = getattr(entity, "username", None)
        title = entity.title or "Untitled"
        members = getattr(entity, "participants_count", None) or ""

        # Build identifier
        if username:
            identifier = f"@{username}"
        else:
            identifier = f"id:{entity.id}"

        about = ""
        if isinstance(entity, Channel):
            try:
                from telethon.tl.functions.channels import GetFullChannelRequest
                full_info = await client(GetFullChannelRequest(entity))
                about = (full_info.full_chat.about or "")[:100].replace("\n", " ")
            except Exception:
                pass

        groups.append({
            "title": title,
            "identifier": identifier,
            "kind": kind,
            "members": members,
            "about": about,
            "unread": dialog.unread_count,
        })

    await client.disconnect()

    # Sort by unread count (most active first)
    groups.sort(key=lambda g: g["unread"], reverse=True)

    # Output markdown table
    channels = [g for g in groups if g["kind"] == "channel"]
    chats = [g for g in groups if g["kind"] == "group"]

    print(f"Found {len(channels)} channels, {len(chats)} groups/supergroups:\n")

    print("## Каналы (broadcast)\n")
    print("| # | Name | ID | Members | Description |")
    print("|---|------|----|---------|-------------|")
    for i, g in enumerate(channels, 1):
        members_str = str(g["members"]) if g["members"] else "?"
        about = g["about"][:80] if g["about"] else ""
        print(f"| {i} | {g['title']} | {g['identifier']} | {members_str} | {about} |")

    print(f"\n## Группы/супергруппы (чаты)\n")
    print("| # | Name | ID | Members | Description |")
    print("|---|------|----|---------|-------------|")
    for i, g in enumerate(chats, 1):
        members_str = str(g["members"]) if g["members"] else "?"
        about = g["about"][:80] if g["about"] else ""
        print(f"| {i} | {g['title']} | {g['identifier']} | {members_str} | {about} |")


if __name__ == "__main__":
    asyncio.run(main())
