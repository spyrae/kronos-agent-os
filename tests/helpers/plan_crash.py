"""Real process-loss windows for the plan claim/turn correlation protocol."""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

mode = sys.argv[1]
root = Path(sys.argv[2]).resolve()
root.mkdir(parents=True, exist_ok=True)
os.environ.update(
    KAOS_ENV_FILE="/dev/null",
    DB_DIR=str(root),
    DB_PATH=str(root / "session.db"),
    SWARM_DB_PATH=str(root / "swarm.db"),
    WORKSPACE_PATH=str(root / "workspace"),
    AGENT_NAME="kronos",
    DEEPSEEK_API_KEY="",
)

from langchain_core.messages import AIMessage

from kronos import bridge, plans
from kronos.cron import plans as poller
from kronos.engine import AgentResult
from kronos.graph import KronosAgent
from kronos.session import SessionStore
from kronos.turn_ownership import own_conversation

agent = object.__new__(KronosAgent)
agent._session_store = SessionStore(str(root / "session.db"))
agent._memory_enabled = False
agent._supervisor = None
agent._tools = []
agent._skill_store = None
agent._system_prompt = "Test"
agent._last_pending_approval_id = None
agent._last_pending_approval_turn_id = None
agent._external_tool_event_callback = None


def kill():
    os.kill(os.getpid(), signal.SIGKILL)


async def model(**kwargs):
    with (root / "models.log").open("a") as stream:
        stream.write("model\n")
    if mode == "crash-model":
        kill()
    if mode == "crash-stop-intent":
        turn = plans.get_step(1)["turn_id"]
        await agent.session_store.begin_external_effect(
            key="stop-effect",
            turn_id=turn,
            tool="write_fake",
            args={},
            tool_call_id="stop-call",
        )
        with (root / "effects.log").open("a") as stream:
            stream.write("effect\n")
        plans.cancel_plan(1, "kronos")
        kill()
    return AgentResult([*kwargs["messages"], AIMessage(content="verified result")], "verified result")


async def crash_before_step_commit(*args):
    kill()


agent._run_model_loop = model
bridge.get_agent = lambda: agent
poller.get_policy = lambda: SimpleNamespace(durable=SimpleNamespace(resume_mode="resume", max_resume_attempts=2))


async def main():
    if mode.startswith("crash-"):
        p = plans.create_plan(agent_name="kronos", goal="crash test")
        s = plans.add_step(p, "work")
        if mode in {"crash-claim", "crash-link", "crash-stop-claim", "crash-stop-link"}:
            async with own_conversation(str(root / "session.db"), f"plan:{p}") as owner:
                assert plans.claim_step(s, ownership=owner)
                if mode in {"crash-link", "crash-stop-link"}:
                    await agent.session_store.begin_turn(
                        f"plan:{p}", "work", caller_key=plans.get_step(s)["execution_key"]
                    )
                if mode.startswith("crash-stop-"):
                    plans.cancel_plan(p, "kronos")
                kill()
        if mode == "crash-finish":
            poller._apply_outcome = crash_before_step_commit
        await poller._run_step(plans.get_plan(p), plans.get_step(s), "")
        raise AssertionError("crash point was not reached")
    await agent.session_store.recover_abandoned_turns()
    await poller._reconcile_stops()
    await poller._reconcile_turns()
    step = plans.get_step(1)
    if step["state"] == plans.STEP_PENDING:
        plans._db().write("UPDATE plan_steps SET wake_at=0 WHERE id=?", (step["id"],))
        await poller._run_step(plans.get_plan(1), plans.get_step(1), "")
    step = plans.get_step(1)
    print(
        json.dumps(
            {
                "state": step["state"],
                "turn_id": step["turn_id"],
                "turn_count": len(await agent.session_store.list_turns()),
            }
        )
    )


asyncio.run(main())
