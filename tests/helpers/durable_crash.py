"""Two halves of a real crash, as separate processes (moat phase 10 acceptance).

The hermetic tests simulate an interruption by leaving a turn `running` in the
database. That proves the recovery logic, not that a process dying mid-flight
leaves the database in the state the recovery logic expects — SQLite WAL, the
aiosqlite connection, and the unflushed writes are all between them.

This module is the missing half. `crash` starts a turn, performs one
side-effecting tool call, and then `SIGKILL`s itself before the turn finishes —
no atexit, no flush, no cleanup. `resume` is a *different* process that picks the
turn up and finishes it.

The side effect is an append to a file, so a duplicate is not a metric to trust
but a second line to see.

Usage (driven by tests/test_durable_kill.py):
    python -m tests.helpers.durable_crash crash  <workdir>
    python -m tests.helpers.durable_crash resume <workdir>
"""

import asyncio
import os
import signal
import sys
from pathlib import Path

THREAD_ID = "kill-test"
QUESTION = "отправь отчёт и подтверди"
TOOL_CALL_ID = "call-1"


def _configure(workdir: Path) -> None:
    """Point this process at the scratch databases before anything imports them."""
    workdir.mkdir(parents=True, exist_ok=True)
    os.environ["KAOS_ENV_FILE"] = "/dev/null"
    os.environ["DB_DIR"] = str(workdir)
    os.environ["DB_PATH"] = str(workdir / "session.db")
    os.environ["SWARM_DB_PATH"] = str(workdir / "swarm.db")
    os.environ["WORKSPACE_PATH"] = str(workdir / "workspace")
    os.environ["AGENT_NAME"] = "killtest"
    os.environ["TOOL_APPROVALS_ENABLED"] = "false"


def _sender(marker: Path):
    """The side-effecting tool: appends a line every time it really runs."""
    from langchain_core.tools import BaseTool

    from kronos.security.effects import mark_side_effect

    class Sender(BaseTool):
        name: str = "send_message"
        description: str = "send the report"

        def _run(self, **kwargs) -> str:
            with open(marker, "a", encoding="utf-8") as handle:
                handle.write(f"sent by pid {os.getpid()}\n")
            return "отчёт отправлен"

    tool = Sender()
    mark_side_effect([tool])
    return tool


async def _crash(workdir: Path) -> None:
    from kronos.engine import execute_tool, side_effect_key
    from kronos.session import SessionStore

    store = SessionStore(str(workdir / "session.db"), agent_name="killtest")
    turn_id = await store.begin_turn(THREAD_ID, QUESTION)

    from langchain_core.messages import AIMessage

    call = {"name": "send_message", "args": {"text": "отчёт"}, "id": TOOL_CALL_ID}
    await store.append_turn_messages(
        turn_id=turn_id,
        thread_id=THREAD_ID,
        messages=[AIMessage(content="", tool_calls=[call])],
    )

    # Perform the real side effect and record it, exactly as react_loop would.
    tool = _sender(workdir / "sent.log")
    message = await execute_tool(
        tool,
        call,
        get_external_effect=store.get_external_effect,
        record_external_effect=lambda key, name, result: store.record_external_effect(
            key=key, turn_id=turn_id, tool=name, result=result
        ),
        turn_id=turn_id,
    )
    await store.save_tool_result(turn_id=turn_id, tool_call_id=TOOL_CALL_ID, content=str(message.content))

    (workdir / "crashed.txt").write_text(
        f"{turn_id}\n{side_effect_key(tool, call['args'], turn_id)}\n",
        encoding="utf-8",
    )

    # Die the way a machine dies: no unwinding, no flush, no goodbye.
    sys.stdout.flush()
    os.kill(os.getpid(), signal.SIGKILL)


async def _resume(workdir: Path) -> int:
    from langchain_core.messages import AIMessage

    import kronos.graph as graph_module
    from kronos.session import SessionStore

    class ScriptedModel:
        """A resumed turn still needs a model to write the final answer."""

        model_name = "scripted"

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages, *args, **kwargs):
            return AIMessage(content="Отчёт отправлен, подтверждаю.")

        def invoke(self, messages, *args, **kwargs):
            return asyncio.get_event_loop().run_until_complete(self.ainvoke(messages))

    graph_module.get_model = lambda tier: ScriptedModel()

    store = SessionStore(str(workdir / "session.db"), agent_name="killtest")
    agent = graph_module.KronosAgent(
        tools=[_sender(workdir / "sent.log")],
        enable_memory=False,
        enable_supervisor=False,
        session_store=store,
    )
    agent._get_system_prompt = lambda: "system"

    delivered: list[str] = []

    async def deliver(thread_id: str, text: str) -> None:
        delivered.append(text)
        with open(workdir / "delivered.log", "a", encoding="utf-8") as handle:
            handle.write(f"{thread_id}\t{text}\n")

    finished = await agent.resume_abandoned_turns(deliver=deliver)
    print(f"finished={finished} delivered={len(delivered)}")
    return finished


def main() -> int:
    mode, workdir = sys.argv[1], Path(sys.argv[2])
    _configure(workdir)

    if mode == "crash":
        asyncio.run(_crash(workdir))
        return 0  # unreachable: the process is killed above
    if mode == "resume":
        return 0 if asyncio.run(_resume(workdir)) else 1
    print(f"unknown mode: {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
