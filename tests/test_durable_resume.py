"""Real resume of interrupted turns (moat phase 10.2).

Until now `recover_abandoned_turns` restored the history and noted the
interruption; the user's question stayed unanswered. Resume finishes the turn —
which is only safe because tool results are memoized per turn and side-effecting
tools consult the effects ledger.
"""

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool

from kronos.config import settings
from kronos.security.effects import mark_side_effect
from kronos.session import SessionStore


class Sender(BaseTool):
    name: str = "send_message"
    description: str = "send"
    calls: int = 0

    def _run(self, **kwargs) -> str:
        self.calls += 1
        return f"sent #{self.calls}"


class ScriptedModel:
    """Returns queued responses; records how many times it was asked."""

    model_name = "scripted"

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, *args, **kwargs):
        self.calls += 1
        if not self._responses:
            return AIMessage(content="нечего добавить")
        return self._responses.pop(0)

    def invoke(self, messages, *args, **kwargs):
        import asyncio

        return asyncio.get_event_loop().run_until_complete(self.ainvoke(messages))


def _agent_with_scripted_model(monkeypatch, store, responses, *, tools=None):
    """Build an agent that never needs a provider key.

    The supervisor is built inside KronosAgent.__init__ and constructs a model,
    so patching after construction is too late — that is what made these tests
    pass locally (where .env has a key) and fail in CI.
    """
    import kronos.graph as graph_module
    from kronos.graph import KronosAgent

    monkeypatch.setattr(graph_module, "get_model", lambda tier: ScriptedModel(responses))
    agent = KronosAgent(
        tools=tools or [],
        enable_memory=False,
        enable_supervisor=False,
        session_store=store,
    )
    monkeypatch.setattr(agent, "_get_system_prompt", lambda: "system")
    return agent


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "tool_approvals_enabled", False)
    import kronos.db as _db
    import kronos.swarm_store as _swarm

    _db._instances.clear()
    _swarm._singleton = None
    yield SessionStore(str(tmp_path / "session.db"), agent_name="test")
    _db._instances.clear()
    _swarm._singleton = None


async def _interrupted_turn(store, *, thread_id="chat-1", text="отправь отчёт"):
    """A turn that started, called a tool, and never finished."""
    turn_id = await store.begin_turn(thread_id, text)
    await store.append_turn_messages(
        turn_id=turn_id,
        thread_id=thread_id,
        messages=[AIMessage(content="", tool_calls=[{"name": "send_message", "args": {"text": "отчёт"}, "id": "c1"}])],
    )
    return turn_id


@pytest.mark.asyncio
async def test_claim_flips_status_and_counts_attempts(store):
    turn_id = await _interrupted_turn(store)

    claimed = await store.claim_turns_for_resume()

    assert [row["turn_id"] for row in claimed] == [turn_id]
    assert claimed[0]["attempts"] == 1
    # A second claim finds nothing: the turn is no longer 'running'.
    assert await store.claim_turns_for_resume() == []


@pytest.mark.asyncio
async def test_attempts_are_capped(store):
    """A turn that keeps dying must not resurrect itself forever."""
    turn_id = await _interrupted_turn(store)

    for _ in range(2):
        await store.claim_turns_for_resume(max_attempts=2)
        # Simulate another crash: back to running with the attempt recorded.
        async with store._open_db() as db:
            await db.execute("UPDATE active_turns SET status = 'running' WHERE turn_id = ?", (turn_id,))
            await db.commit()

    assert await store.claim_turns_for_resume(max_attempts=2) == []

    async with store._open_db() as db:
        cursor = await db.execute("SELECT status, error FROM active_turns WHERE turn_id = ?", (turn_id,))
        status, error = await cursor.fetchone()
    assert status == "failed"
    assert "gave up" in error


@pytest.mark.asyncio
async def test_superseded_turn_is_not_resumed(store):
    """If the user asked again, answering the stale question is noise."""
    stale = await _interrupted_turn(store, text="первый вопрос")
    fresh = await store.begin_turn("chat-1", "второй вопрос")

    claimed = await store.claim_turns_for_resume()

    assert [row["turn_id"] for row in claimed] == [fresh]
    async with store._open_db() as db:
        cursor = await db.execute("SELECT status FROM active_turns WHERE turn_id = ?", (stale,))
        (status,) = await cursor.fetchone()
    assert status == "superseded"


@pytest.mark.asyncio
async def test_resume_finishes_the_turn_and_delivers(store, monkeypatch):
    turn_id = await _interrupted_turn(store)
    sender = Sender()
    mark_side_effect([sender])

    agent = _agent_with_scripted_model(monkeypatch, store, [AIMessage(content="Отчёт отправлен.")], tools=[sender])

    delivered: list[tuple[str, str]] = []

    async def deliver(thread_id, text):
        delivered.append((thread_id, text))

    finished = await agent.resume_abandoned_turns(deliver=deliver)

    assert finished == 1
    assert delivered and delivered[0][0] == "chat-1"
    async with store._open_db() as db:
        cursor = await db.execute("SELECT status FROM active_turns WHERE turn_id = ?", (turn_id,))
        (status,) = await cursor.fetchone()
    assert status in {"completed", "done", "finished"}


@pytest.mark.asyncio
async def test_resume_does_not_repeat_a_recorded_side_effect(store, monkeypatch):
    """The whole reason the ledger landed first."""
    from kronos.engine import side_effect_key

    turn_id = await _interrupted_turn(store)
    sender = Sender()
    mark_side_effect([sender])

    # The crash happened after the send but before the result was journalled.
    key = side_effect_key(sender, {"text": "отчёт"}, turn_id)
    await store.record_external_effect(key=key, turn_id=turn_id, tool=sender.name, result="sent #1")

    agent = _agent_with_scripted_model(monkeypatch, store, [AIMessage(content="Готово.")], tools=[sender])

    await agent.resume_abandoned_turns()

    assert sender.calls == 0, "a message already sent must not be sent again"


@pytest.mark.asyncio
async def test_report_mode_keeps_the_old_behaviour(store):
    """Default stays report so an upgrade does not change what a restart does."""
    turn_id = await _interrupted_turn(store)

    recovered = await store.recover_abandoned_turns()

    assert recovered == 1
    async with store._open_db() as db:
        cursor = await db.execute("SELECT status FROM active_turns WHERE turn_id = ?", (turn_id,))
        (status,) = await cursor.fetchone()
    assert status == "recovered"


@pytest.mark.asyncio
async def test_delivery_failure_still_completes_the_turn(store, monkeypatch):
    await _interrupted_turn(store)
    agent = _agent_with_scripted_model(monkeypatch, store, [AIMessage(content="Ответ.")])

    async def broken_deliver(thread_id, text):
        raise RuntimeError("telegram down")

    finished = await agent.resume_abandoned_turns(deliver=broken_deliver)

    assert finished == 1  # the answer exists and is journalled; only delivery failed


def test_policy_exposes_resume_mode(tmp_path, monkeypatch):
    import yaml

    from kronos.policy import ENV_POLICY_FILE, PolicyError, load_policy, reset_policy

    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump({"durable": {"resume_mode": "resume", "max_resume_attempts": 3}}), encoding="utf-8")
    monkeypatch.setenv(ENV_POLICY_FILE, str(path))
    reset_policy()

    policy = load_policy()
    assert policy.durable.resume_mode == "resume"
    assert policy.durable.max_resume_attempts == 3

    path.write_text(yaml.safe_dump({"durable": {"resume_mode": "maybe"}}), encoding="utf-8")
    reset_policy()
    with pytest.raises(PolicyError, match="resume_mode must be"):
        load_policy()
    reset_policy()
