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

THREAD_ID = "77001"
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


async def _crash(
    workdir: Path, *, before_result: bool = False, before_dispatch: bool = False, before_cache: bool = False
) -> None:
    from kronos.engine import execute_tool, side_effect_key
    from kronos.session import SessionStore

    store = SessionStore(str(workdir / "session.db"), agent_name="killtest")
    from kronos.turn_delivery import RecoveryDestination

    turn_id = await store.begin_turn(THREAD_ID, QUESTION, recovery_destination=RecoveryDestination(77001, 55))

    from langchain_core.messages import AIMessage

    call = {"name": "send_message", "args": {"text": "отчёт"}, "id": TOOL_CALL_ID}
    await store.append_turn_messages(
        turn_id=turn_id,
        thread_id=THREAD_ID,
        messages=[AIMessage(content="", tool_calls=[call])],
    )

    # Perform the real side effect and record it, exactly as react_loop would.
    tool = _sender(workdir / "sent.log")

    def kill_with_marker():
        (workdir / "crashed.txt").write_text(
            f"{turn_id}\n{side_effect_key(tool, call['args'], turn_id)}\n", encoding="utf-8"
        )
        os.kill(os.getpid(), signal.SIGKILL)

    if before_dispatch:
        kill_with_marker()

    async def finish_effect(key, token, name, result):
        if before_result:
            (workdir / "crashed.txt").write_text(f"{turn_id}\n{key}\n", encoding="utf-8")
            os.kill(os.getpid(), signal.SIGKILL)
        await store.finish_external_effect(key=key, token=token, turn_id=turn_id, tool=name, result=result)

    message = await execute_tool(
        tool,
        call,
        begin_external_effect=lambda key, name, args, call_id, dedupe_by_key: store.begin_external_effect(
            key=key, turn_id=turn_id, tool=name, args=args, tool_call_id=call_id, dedupe_by_key=dedupe_by_key
        ),
        finish_external_effect=finish_effect,
        turn_id=turn_id,
    )
    if before_cache:
        kill_with_marker()
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
            from langchain_core.messages import ToolMessage

            pending = set()
            for message in messages:
                if isinstance(message, ToolMessage):
                    assert message.tool_call_id in pending, "orphan tool result on real restart"
                    pending.remove(message.tool_call_id)
                else:
                    assert not pending, "incomplete batch reached model on real restart"
                    pending = {call["id"] for call in getattr(message, "tool_calls", [])}
            assert not pending, "unanswered tool call reached model on real restart"
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

    from kronos import telegram_delivery

    delivered: list[str] = []

    async def deliver(chunk) -> int:
        delivered.append(chunk.text)
        with open(workdir / "delivered.log", "a", encoding="utf-8") as handle:
            handle.write(f"{chunk.chat_id}\t{chunk.text}\n")
        return 100 + len(delivered)

    telegram_delivery.ready_sender = lambda: 55
    telegram_delivery.send_chunk = deliver
    finished = await agent.resume_abandoned_turns()
    await store.deliver_pending()
    print(f"finished={finished} delivered={len(delivered)}")
    return finished


async def _hold(workdir: Path, *, resume: bool) -> None:
    """Keep a real executor alive with its event loop intentionally blocked."""
    import time

    from kronos.engine import AgentResult
    from kronos.graph import KronosAgent
    from kronos.session import SessionStore

    store = SessionStore(str(workdir / "session.db"), agent_name="killtest")
    agent = object.__new__(KronosAgent)
    agent._session_store = store
    agent._memory_enabled = False
    agent._durable_recovery_checked = True

    async def blocked_loop(**kwargs):
        turn = (await store.resumable_turns())[0]
        (workdir / "holding.txt").write_text(turn["turn_id"], encoding="utf-8")
        time.sleep(120)
        return AgentResult(content="unreachable", messages=[])

    agent._run_model_loop = blocked_loop
    from kronos.turn_delivery import RecoveryDestination

    destination = RecoveryDestination(77001, 55)
    if resume:
        turn_id = await store.begin_turn(THREAD_ID, QUESTION, recovery_destination=destination)
        await agent.resume_interrupted_turn(turn_id)
    else:
        await agent.ainvoke_outcome(QUESTION, THREAD_ID, recovery_destination=destination)


async def _report(workdir: Path) -> int:
    from kronos.session import SessionStore

    count = await SessionStore(str(workdir / "session.db")).recover_abandoned_turns()
    print(f"recovered={count}")
    return 0


async def _owned_crash(workdir: Path, *, mode: str) -> None:
    from kronos.turn_ownership import own_conversation

    async with own_conversation(str(workdir / "session.db"), THREAD_ID):
        await _crash(
            workdir,
            before_result=mode == "crash-before-result",
            before_dispatch=mode == "crash-before-dispatch",
            before_cache=mode == "crash-before-cache",
        )


def main() -> int:
    mode, workdir = sys.argv[1], Path(sys.argv[2])
    _configure(workdir)

    if mode in {"crash", "crash-before-result", "crash-before-dispatch", "crash-before-cache"}:
        asyncio.run(_owned_crash(workdir, mode=mode))
        return 0  # unreachable: the process is killed above
    if mode == "resume":
        return 0 if asyncio.run(_resume(workdir)) else 1
    if mode in {"hold-live", "hold-resume"}:
        asyncio.run(_hold(workdir, resume=mode == "hold-resume"))
        return 0
    if mode == "report":
        return asyncio.run(_report(workdir))
    print(f"unknown mode: {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
