"""Child-process outbox crash windows with an idempotent local fake provider."""

import asyncio
import json
import os
import signal
import sqlite3
import sys
from pathlib import Path

mode, directory = sys.argv[1:]
root = Path(directory)
os.environ.update(KAOS_ENV_FILE="/dev/null", DB_DIR=str(root), DB_PATH=str(root / "session.db"), AGENT_NAME="kronos")

from kronos import plans
from kronos.delivery import drain


def kill():
    os.kill(os.getpid(), signal.SIGKILL)


async def main():
    db = plans._db()
    if mode.startswith("crash"):
        plan_id = plans.create_plan(agent_name="kronos", goal="delivery crash test", chat_id=77)
        step = plans.add_step(plan_id, "work")
        plans.finish_step(step, "result")
        plans.settle_plan(plan_id)
        if mode == "crash-producer":
            original = plans.enqueue

            def die(conn, **kwargs):
                original(conn, **kwargs)
                kill()

            plans.enqueue = die
        plans.set_summary(plan_id, "x" * 3600)
        if mode == "crash-queued":
            kill()
    else:
        plan_id = plans.list_plans("kronos", state="done")[0]["id"]
        if not plans.get_plan(plan_id)["summary"]:
            plans.set_summary(plan_id, "x" * 3600)

    calls = 0

    async def provider(chunk):
        nonlocal calls
        calls += 1
        if mode == "crash-first-ack" and calls == 2:
            kill()
        with sqlite3.connect(root / "provider.db") as external:
            external.execute(
                "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, random_id INTEGER UNIQUE, text TEXT)"
            )
            external.execute(
                "INSERT OR IGNORE INTO messages(random_id, text) VALUES (?, ?)", (chunk.random_id, chunk.text)
            )
            receipt = external.execute(
                "SELECT id, text FROM messages WHERE random_id = ?", (chunk.random_id,)
            ).fetchone()
            assert receipt[1] == chunk.text
        with (root / "requests.log").open("a") as stream:
            stream.write(str(chunk.random_id) + "\n")
        if mode == "crash-sent":
            kill()
        return receipt[0]

    if mode == "crash-final-ack":
        original_write = db.write

        def write(sql, params=()):
            result = original_write(sql, params)
            if "SET next_chunk" in sql and "delivered" in params:
                kill()
            return result

        db.write = write
    await drain(db, sender_id=55, send=provider)
    print(json.dumps(plans.delivery_status(plans.get_plan(plan_id))))


asyncio.run(main())
