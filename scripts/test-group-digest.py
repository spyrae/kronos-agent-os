#!/usr/bin/env python3
"""Test run of group digest cron job.

Usage:
    cd /opt/kronos-ii/app && .venv/bin/python scripts/test-group-digest.py
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

# Force userbot session (not bot) for Telethon client in telegram_client.py
os.environ["USERBOT_SESSION"] = "userbot"

from kronos.cron.group_digest import run_group_digest


if __name__ == "__main__":
    asyncio.run(run_group_digest())
