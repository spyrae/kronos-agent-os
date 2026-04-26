"""One-time backfill: index existing session messages into swarm FTS."""

import asyncio
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kronos.config import settings
from kronos.swarm_store import get_swarm


async def backfill():
    """Scan all per-agent session.db files, extract messages, index to swarm FTS."""
    data_dir = Path(settings.db_dir).parent  # data/
    swarm = get_swarm()
    total = 0

    for agent_dir in data_dir.iterdir():
        if not agent_dir.is_dir():
            continue
        session_db = agent_dir / "session.db"
        if not session_db.exists():
            continue

        agent_name = agent_dir.name
        print(f"Processing {agent_name}...")

        import aiosqlite

        async with aiosqlite.connect(str(session_db), timeout=30) as db:
            try:
                cursor = await db.execute("SELECT thread_id, messages FROM sessions")
                rows = await cursor.fetchall()
            except Exception as e:
                print(f"  Error reading {agent_name}: {e}")
                continue

        for thread_id, messages_json in rows:
            try:
                messages = json.loads(messages_json)
            except json.JSONDecodeError:
                continue

            for msg in messages:
                role_map = {"HumanMessage": "user", "AIMessage": "assistant"}
                role = role_map.get(msg.get("type"), None)
                content = msg.get("content", "")
                if role and content and len(content) > 5:
                    swarm.index_session_message(
                        agent_name=agent_name,
                        thread_id=thread_id,
                        role=role,
                        content=content,
                    )
                    total += 1

    print(f"Backfill complete: {total} messages indexed")


if __name__ == "__main__":
    asyncio.run(backfill())
