"""Crash recovery must finish the frozen batch before asking the model again."""

import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from kronos.config import settings
from kronos.effect_state import DurableStateError
from kronos.engine import react_loop, side_effect_key
from kronos.graph import KronosAgent
from kronos.session import SessionStore


def _call(name="send_report", call_id="c1", text="original"):
    return {"name": name, "id": call_id, "args": {"text": text}}


def _assert_model_protocol(messages):
    # Independent provider-shaped check, not the production validator.
    required = set()
    for message in messages:
        if isinstance(message, ToolMessage):
            assert message.tool_call_id in required, "orphan/duplicate tool result sent to model"
            required.remove(message.tool_call_id)
        else:
            assert not required, "another message interrupted an unfinished tool batch"
            if isinstance(message, AIMessage):
                required = {call["id"] for call in message.tool_calls}
                assert len(required) == len(message.tool_calls)
    assert not required, "model received unanswered tool calls"


class StrictModel:
    model_name = "strict-mock"

    def __init__(self, *responses):
        self.responses = list(responses) or [AIMessage(content="done")]
        self.requests = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, **kwargs):
        _assert_model_protocol(messages)
        self.requests.append(list(messages))
        assert self.responses, "unexpected extra model call"
        return self.responses.pop(0)


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "tool_approvals_enabled", False)
    import kronos.db as db_module

    db_module._instances.clear()
    store = SessionStore(settings.db_path)
    agent = object.__new__(KronosAgent)
    agent._session_store = store
    agent._memory_enabled = False
    agent._supervisor = None
    agent._tools = []
    agent._skill_store = None
    agent._system_prompt = "system"
    agent._external_tool_event_callback = None
    agent._durable_recovery_checked = True
    agent._last_pending_approval_id = None
    turn = await store.begin_turn("chat", "work")
    model = StrictModel()
    monkeypatch.setattr("kronos.graph.get_model", lambda tier: model)
    yield agent, store, turn, model
    db_module._instances.clear()


def _tool(name="send_report"):
    effect = AsyncMock(return_value="actual result")

    async def run(text: str) -> str:
        return await effect(text)

    return StructuredTool.from_function(coroutine=run, name=name, description="mock tool"), effect


async def _journal(store, turn, *messages):
    await store.append_turn_messages(turn_id=turn, thread_id="chat", messages=list(messages))


async def _record(store, turn, call, result="recorded result"):
    key = f"business-key:{call['id']}"
    claim = await store.begin_external_effect(
        key=key, turn_id=turn, tool=call["name"], args=call["args"], tool_call_id=call["id"], dedupe_by_key=True
    )
    await store.finish_external_effect(key=key, token=claim.token, turn_id=turn, tool=call["name"], result=result)


async def test_unstarted_batch_runs_original_calls_before_model(runtime):
    agent, store, turn, model = runtime
    tool, effect = _tool()
    agent._tools = [tool]
    await _journal(store, turn, AIMessage(content="", tool_calls=[_call()]))
    assert await agent.resume_interrupted_turn(turn) == "done"
    effect.assert_awaited_once_with("original")
    sent = model.requests[0]
    assert [m.tool_calls for m in sent if isinstance(m, AIMessage)] == [[_call() | {"type": "tool_call"}]]
    assert [m.content for m in sent if isinstance(m, ToolMessage)] == ["actual result"]
    assert len(model.requests) == 1


async def test_partial_batch_keeps_journalled_results_and_runs_only_missing_calls(runtime):
    agent, store, turn, model = runtime
    writer, wrote = _tool()
    reader, read = _tool("get_status")
    agent._tools = [writer, reader]
    await _journal(
        store, turn,
        AIMessage(content="", tool_calls=[_call(), _call("get_status", "c2", "lookup")]),
        ToolMessage(content="already sent", tool_call_id="c1"),
    )
    assert await agent.resume_interrupted_turn(turn) == "done"
    wrote.assert_not_awaited()
    read.assert_awaited_once_with("lookup")
    assert [m.tool_call_id for m in model.requests[0] if isinstance(m, ToolMessage)] == ["c1", "c2"]


@pytest.mark.parametrize("source", ["cache", "effect"])
async def test_recorded_result_restores_even_when_tool_was_removed(runtime, source):
    agent, store, turn, model = runtime
    await _journal(store, turn, AIMessage(content="", tool_calls=[_call()]))
    if source == "cache":
        await store.save_tool_result(turn_id=turn, tool_call_id="c1", content="recorded result")
    else:
        await _record(store, turn, _call())
    assert agent._tools == []
    assert await agent.resume_interrupted_turn(turn) == "done"
    result = next(m for m in model.requests[0] if isinstance(m, ToolMessage))
    assert "recorded result" in result.content
    assert "Unknown tool" not in result.content
    assert "UNTRUSTED" in result.content


async def test_committed_effect_does_not_ask_for_approval_again(runtime, monkeypatch):
    agent, store, turn, model = runtime
    monkeypatch.setattr(settings, "tool_approvals_enabled", True)
    tool, effect = _tool()
    agent._tools = [tool]
    await _journal(store, turn, AIMessage(content="", tool_calls=[_call()]))
    await _record(store, turn, _call())
    assert await agent.resume_interrupted_turn(turn) == "done"
    effect.assert_not_awaited()
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM pending_approvals").fetchone()[0] == 0


async def test_recorded_result_stays_untrusted_during_recovery(runtime):
    agent, store, turn, model = runtime
    tool, effect = _tool()
    tool.metadata = {"side_effect": True, "untrusted_output": True}
    agent._tools = [tool]
    await _journal(store, turn, AIMessage(content="", tool_calls=[_call()]))
    await _record(store, turn, _call(), "remote data")
    assert await agent.resume_interrupted_turn(turn) == "done"
    message = next(m for m in model.requests[0] if isinstance(m, ToolMessage))
    assert "UNTRUSTED" in message.content
    effect.assert_not_awaited()


async def test_recovered_call_identity_must_match_tool_and_arguments(runtime):
    _, store, turn, _ = runtime
    await _record(store, turn, _call())
    assert await store.get_recorded_call_effect(turn, _call()) == "recorded result"
    assert await store.get_recorded_call_effect(turn, _call(text="different")) is None
    assert await store.get_recorded_call_effect(turn, _call(name="different_tool")) is None
    assert await store.get_recorded_call_effect("different-turn", _call()) is None


async def test_legacy_missing_intent_does_not_authorize_dispatch(runtime):
    agent, store, turn, model = runtime
    tool, effect = _tool()
    agent._tools = [tool]
    await _journal(store, turn, AIMessage(content="", tool_calls=[_call()]))
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE active_turns SET effect_protocol = 0 WHERE turn_id = ?", (turn,))
    assert await agent.resume_interrupted_turn(turn) is None
    assert model.requests == []
    effect.assert_not_awaited()
    assert "legacy turn" in (await store.get_turn_detail(turn))["error"]


async def test_legacy_recorded_result_is_reused_without_dispatch_or_approval(runtime, monkeypatch):
    agent, store, turn, model = runtime
    monkeypatch.setattr(settings, "tool_approvals_enabled", True)
    tool, effect = _tool()
    agent._tools = [tool]
    await _journal(store, turn, AIMessage(content="", tool_calls=[_call()]))
    await store.record_external_effect(
        key=side_effect_key(tool, _call()["args"], turn), turn_id=turn, tool=tool.name, result="old result"
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE active_turns SET effect_protocol = 0 WHERE turn_id = ?", (turn,))
    assert await agent.resume_interrupted_turn(turn) == "done"
    effect.assert_not_awaited()
    assert any(isinstance(m, ToolMessage) and m.content == "old result" for m in model.requests[0])


async def test_legacy_read_only_call_can_resume(runtime):
    agent, store, turn, model = runtime
    tool, read = _tool("get_status")
    agent._tools = [tool]
    await _journal(store, turn, AIMessage(content="", tool_calls=[_call("get_status")]))
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE active_turns SET effect_protocol = 0 WHERE turn_id = ?", (turn,))
    assert await agent.resume_interrupted_turn(turn) == "done"
    read.assert_awaited_once()


async def test_replay_pauses_before_new_effect_and_approval_continuation_is_valid(runtime, monkeypatch):
    agent, store, turn, model = runtime
    monkeypatch.setattr(settings, "tool_approvals_enabled", True)
    tool, effect = _tool()
    agent._tools = [tool]
    await _journal(store, turn, AIMessage(content="", tool_calls=[_call(), _call(call_id="c2", text="later")]))
    assert "Approval ID" in await agent.resume_interrupted_turn(turn)
    assert model.requests == []
    effect.assert_not_awaited()
    outcome = await store.get_turn_outcome(turn)
    assert outcome.status == "waiting_approval"
    assert await agent.resolve_tool_approval(outcome.approval_id, True) == "done"
    effect.assert_awaited_once_with("original")
    assert len(model.requests) == 1
    assert [m.tool_call_id for m in model.requests[0] if isinstance(m, ToolMessage)] == ["c2", "c1"]


async def test_replayed_batch_does_not_use_up_the_model_call_budget(runtime):
    agent, store, turn, model = runtime
    tool, effect = _tool("get_status")
    await _journal(store, turn, AIMessage(content="", tool_calls=[_call("get_status")]))
    messages = await store.load_turn_messages("chat", turn)
    result = await react_loop(
        model, messages, [tool], max_turns=1, resume_pending_tools=True,
        **agent._build_durable_react_loop_kwargs(turn_id=turn, thread_id="chat"),
    )
    assert result.content == "done"
    assert len(model.requests) == 1
    effect.assert_awaited_once()


@pytest.mark.parametrize("raw", [
    "{", "[]", '{"type":"UnknownMessage","content":"x"}',
    '{"type":"AIMessage","content":"","tool_calls":{}}',
    '{"type":"ToolMessage","content":"x","tool_call_id":"orphan"}',
    '{"type":"AIMessage","content":"","tool_calls":[{"name":"send_report","id":"c1","args":{"text":NaN}}]}',
])
async def test_corrupt_journal_is_not_skipped_into_a_successful_resume(runtime, raw):
    agent, store, turn, model = runtime
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT INTO turn_journal(turn_id,thread_id,seq,message_json) VALUES (?, 'chat', 1, ?)", (turn, raw)
        )
    assert await agent.resume_interrupted_turn(turn) is None
    assert model.requests == []
    assert "invalid durable journal" in (await store.get_turn_detail(turn))["error"]
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT message_json FROM turn_journal").fetchone()[0] == raw


async def test_non_final_unanswered_batch_is_not_executed(runtime):
    agent, store, turn, model = runtime
    tool, effect = _tool()
    agent._tools = [tool]
    await _journal(store, turn, AIMessage(content="", tool_calls=[_call()]), AIMessage(content="not evidence"))
    assert await agent.resume_interrupted_turn(turn) is None
    effect.assert_not_awaited()
    assert model.requests == []


async def test_duplicate_ids_in_batch_stop_before_any_dispatch(runtime):
    agent, store, turn, model = runtime
    tool, effect = _tool()
    agent._tools = [tool]
    await _journal(store, turn, AIMessage(content="", tool_calls=[_call(), _call(text="second")]))
    assert await agent.resume_interrupted_turn(turn) is None
    effect.assert_not_awaited()
    assert model.requests == []


async def test_result_journal_failure_during_replay_stops_before_model(runtime, monkeypatch):
    agent, store, turn, model = runtime
    tool, effect = _tool()
    agent._tools = [tool]
    await _journal(store, turn, AIMessage(content="", tool_calls=[_call()]))
    monkeypatch.setattr(store, "append_turn_messages", AsyncMock(side_effect=sqlite3.OperationalError("disk full")))
    assert await agent.resume_interrupted_turn(turn) is None
    effect.assert_awaited_once()
    assert model.requests == []
    assert (await store.list_external_effects(turn))[0]["status"] == "recorded"


async def test_report_closes_unanswered_protocol_without_forging_effect_evidence(runtime):
    agent, store, turn, model = runtime
    await _journal(store, turn, AIMessage(content="", tool_calls=[_call()]))
    assert await store.recover_abandoned_turns() == 1
    history = await store.load("chat")
    _assert_model_protocol(history)
    marker = next(m for m in history if isinstance(m, ToolMessage))
    assert "NO VERIFIED RESULT" in marker.content
    assert await store.get_tool_result(turn, "c1") is None
    assert await store.list_external_effects(turn) == []
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM turn_journal").fetchone()[0] == 1
    assert (await agent.ainvoke_outcome("unrelated question", "chat")).content == "done"
    assert len(model.requests) == 1


async def test_report_does_not_overwrite_history_after_corrupt_journal(runtime):
    _, store, turn, _ = runtime
    await store.save("chat", [HumanMessage(content="previous context")])
    with sqlite3.connect(store.db_path) as db:
        db.execute("INSERT INTO turn_journal(turn_id,thread_id,seq,message_json) VALUES (?, 'chat', 1, '[')", (turn,))
    assert await store.recover_abandoned_turns() == 0
    assert [m.content for m in await store.load("chat")] == ["previous context"]
    assert (await store.get_turn_detail(turn))["status"] == "failed"


async def test_changed_arguments_under_cached_call_id_are_not_mistaken_for_retry(runtime):
    agent, store, turn, model = runtime
    tool, effect = _tool()
    agent._tools = [tool]
    await _journal(store, turn, AIMessage(content="", tool_calls=[_call()]))
    await store.save_tool_result(turn_id=turn, tool_call_id="c1", content="first result")
    model.responses = [AIMessage(content="", tool_calls=[_call(text="different action")])]
    assert await agent.resume_interrupted_turn(turn) is None
    effect.assert_not_awaited()
    assert "reused with different arguments" in (await store.get_turn_detail(turn))["error"]


async def test_migration_keeps_legacy_unknown_and_marks_only_new_turns(tmp_path):
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE active_turns (
            turn_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, status TEXT NOT NULL,
            input_message TEXT NOT NULL, started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP, error TEXT)""")
        db.execute("INSERT INTO active_turns(turn_id,thread_id,status,input_message) VALUES ('old','chat','running','work')")
    await asyncio.gather(*(SessionStore(path).begin_turn(f"thread-{i}", "new") for i in range(4)))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT effect_protocol FROM active_turns WHERE turn_id = 'old'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM active_turns WHERE effect_protocol = 1").fetchone()[0] == 4


async def test_plain_loop_cannot_silently_replay_unfinished_history(runtime):
    _, _, _, model = runtime
    with pytest.raises(DurableStateError, match="explicit durable resume"):
        await react_loop(model, [HumanMessage(content="work"), AIMessage(content="", tool_calls=[_call()])], [])
    assert model.requests == []


@pytest.mark.parametrize("adapter", ["openai", "deepseek"])
async def test_real_adapter_serializes_complete_recovered_batch_to_mock_http(runtime, monkeypatch, adapter):
    import httpx
    from langchain_deepseek import ChatDeepSeek
    from langchain_openai import ChatOpenAI

    agent, store, turn, _ = runtime
    tool, effect = _tool()
    agent._tools = [tool]
    await _journal(store, turn, AIMessage(content="", tool_calls=[_call()]))
    await _record(store, turn, _call())
    requests = []

    def provider(request):
        body = json.loads(request.content)
        required = set()
        for message in body["messages"]:
            if message["role"] == "tool":
                assert message["tool_call_id"] in required
                required.remove(message["tool_call_id"])
            else:
                assert not required, "invalid role ordering in actual adapter HTTP payload"
                required = {call["id"] for call in message.get("tool_calls", [])}
        assert not required, "actual adapter request has missing tool results"
        assert any(m["role"] == "tool" and "recorded result" in m["content"] for m in body["messages"])
        requests.append(body)
        return httpx.Response(200, json={
            "id": "mock-completion", "object": "chat.completion", "created": 0,
            "model": "deepseek-chat" if adapter == "deepseek" else "mock-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "adapter done"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        model_type = ChatDeepSeek if adapter == "deepseek" else ChatOpenAI
        model = model_type(
            model="deepseek-chat" if adapter == "deepseek" else "mock-model", api_key="mock-token",
            base_url="https://mock.invalid/v1", http_async_client=client, max_retries=0,
        )
        monkeypatch.setattr("kronos.graph.get_model", lambda tier: model)
        assert await agent.resume_interrupted_turn(turn) == "adapter done"
    assert len(requests) == 1
    effect.assert_not_awaited()


@pytest.mark.parametrize("kind", ["delegate", "unclassified"])
async def test_unrecorded_custom_pipeline_is_not_blindly_replayed(runtime, kind):
    agent, store, turn, model = runtime
    name = "delegate_to_custom" if kind == "delegate" else "calculate_and_save"
    tool, effect = _tool(name)
    if kind == "delegate":
        tool.metadata = {"delegates": True}
    agent._tools = [tool]
    await _journal(store, turn, AIMessage(content="", tool_calls=[_call(name)]))
    assert await agent.resume_interrupted_turn(turn) is None
    assert "safe replay contract" in (await store.get_turn_detail(turn))["error"]
    effect.assert_not_awaited()
    assert model.requests == []


async def test_explicitly_read_only_custom_tool_can_resume(runtime):
    agent, store, turn, model = runtime
    tool, effect = _tool("calculate_summary")
    tool.metadata = {"side_effect": False}
    agent._tools = [tool]
    await _journal(store, turn, AIMessage(content="", tool_calls=[_call(tool.name)]))
    assert await agent.resume_interrupted_turn(turn) == "done"
    effect.assert_awaited_once()


async def test_completed_delegate_result_is_restored_without_reentering_pipeline(runtime):
    agent, store, turn, model = runtime
    tool, effect = _tool("delegate_to_custom")
    tool.metadata = {"delegates": True}
    agent._tools = [tool]
    await _journal(store, turn, AIMessage(content="", tool_calls=[_call(tool.name)]))
    await store.save_tool_result(turn_id=turn, tool_call_id="c1", content="child result")
    assert await agent.resume_interrupted_turn(turn) == "done"
    effect.assert_not_awaited()


@pytest.mark.parametrize("entrypoint", ["save", "finalize"])
async def test_completed_history_limit_does_not_split_a_tool_batch(runtime, entrypoint):
    _, store, turn, _ = runtime
    calls = [_call("get_status", f"old-{i}") for i in range(40)]
    messages = [HumanMessage(content="old question"), AIMessage(content="", tool_calls=calls)]
    messages += [ToolMessage(content="old result", tool_call_id=call["id"]) for call in calls]
    messages += [AIMessage(content="old done"), HumanMessage(content="recent question"), AIMessage(content="recent done")]
    if entrypoint == "save":
        await store.save("chat", messages)
    else:
        await store.finalize_turn(thread_id="chat", turn_id=turn, messages=messages, content="recent done")
    history = await store.load("chat")
    assert [m.content for m in history] == ["old done", "recent question", "recent done"]
    _assert_model_protocol(history)
    with sqlite3.connect(store.db_path) as db:
        saved = json.loads(db.execute("SELECT messages FROM sessions WHERE thread_id = 'chat'").fetchone()[0])
    assert saved[0]["type"] == "AIMessage", "persisted history must not begin with orphan tool results"


async def test_legacy_trimmed_prefix_is_omitted_on_read_without_changing_source(runtime):
    _, store, turn, _ = runtime
    raw = json.dumps([
        {"type": "ToolMessage", "content": "old orphan", "tool_call_id": "old-id"},
        {"type": "AIMessage", "content": "old completed answer"},
    ])
    with sqlite3.connect(store.db_path) as db:
        db.execute("INSERT INTO sessions(thread_id,messages) VALUES ('chat', ?)", (raw,))
    history = await store.load_turn_messages("chat", turn)
    assert [m.content for m in history] == ["old completed answer", "work"]
    _assert_model_protocol(history)
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT messages FROM sessions WHERE thread_id = 'chat'").fetchone()[0] == raw
