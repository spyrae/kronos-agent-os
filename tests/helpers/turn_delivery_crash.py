"""SIGKILL at real session/queue transactions, with a local idempotent transport."""

import asyncio
import json
import os
import signal
import sqlite3
import sys
from pathlib import Path

mode, directory = sys.argv[1:]
root = Path(directory)
os.environ.update(
    KAOS_ENV_FILE="/dev/null",
    DB_DIR=str(root),
    DB_PATH=str(root / "session.db"),
    SWARM_DB_PATH=str(root / "swarm.db"),
    WORKSPACE_PATH=str(root / "workspace"),
    AGENT_NAME="kronos",
)

from langchain_core.messages import AIMessage, HumanMessage

from kronos import graph, session, telegram_delivery
from kronos.db import SafeDB
from kronos.turn_delivery import RecoveryDestination


def kill():
    os.kill(os.getpid(), signal.SIGKILL)


class Model:
    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        with (root / "model.log").open("a") as file:
            file.write("model call\n")
        return AIMessage(content="x" * 3600)


async def main():
    store = session.SessionStore(str(root / "session.db"))
    graph.KronosAgent._init_tools = lambda self: None
    graph.get_model = lambda tier: Model()
    agent = graph.KronosAgent(session_store=store, enable_memory=False, enable_supervisor=False)
    agent._system_prompt = "system"
    if mode.startswith("crash"):
        turn = await store.begin_turn("77", "question", recovery_destination=RecoveryDestination(77, 55))
        (root / "turn.txt").write_text(turn)
        await store.append_turn_messages(turn_id=turn, thread_id="77", messages=[HumanMessage(content="question")])
    else:
        turn = (root / "turn.txt").read_text()
    if mode == "crash-producer":
        original = session.queue_result

        async def die(*args, **kwargs):
            await original(*args, **kwargs)
            kill()

        session.queue_result = die
    await agent.resume_abandoned_turns()
    if mode == "crash-queued":
        kill()
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
            receipt, text = external.execute(
                "SELECT id, text FROM messages WHERE random_id = ?", (chunk.random_id,)
            ).fetchone()
            assert text == chunk.text
        with (root / "requests.log").open("a") as stream:
            stream.write(str(chunk.random_id) + "\n")
        if mode == "crash-sent":
            kill()
        return receipt

    if mode == "crash-final-ack":
        original_write = SafeDB.write

        def write(self, sql, params=()):
            result = original_write(self, sql, params)
            if "SET next_chunk" in sql and "delivered" in params:
                kill()
            return result

        SafeDB.write = write
    telegram_delivery.ready_sender = lambda: 55
    telegram_delivery.send_chunk = provider
    await store.deliver_pending()
    print(json.dumps(await store.delivery_status(turn)))


asyncio.run(main())
