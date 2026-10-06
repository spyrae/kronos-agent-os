"""F08/F10: only an operator, holding the conversation, settles an effect with an unknown outcome."""

import sqlite3

import aiosqlite
import pytest
from langchain_core.messages import AIMessage

from kronos.config import settings
from kronos.effect_state import DurableStateError, EffectUncertainError
from kronos.migrations.v010_effect_reconciliations import migrate
from kronos.session import SessionStore
from kronos.turn_ownership import TurnBusyError, own_conversation

ARGS = {"to": "bank", "amount": 100}


@pytest.fixture
async def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    import kronos.db as db_module
    import kronos.swarm_store as swarm_module

    db_module._instances.clear()
    swarm_module._singleton = None
    yield SessionStore(str(tmp_path / "session.db"))
    db_module._instances.clear()
    swarm_module._singleton = None


async def _dispatched(store, *, thread="thread", key="pay-1", tool="send_payment"):
    """A turn that dispatched an effect and never learned how it ended."""
    turn = await store.begin_turn(thread, "pay the bank")
    await store.begin_external_effect(key=key, turn_id=turn, tool=tool, args=ARGS, tool_call_id="c1")
    return turn


async def _reconcile(store, key="pay-1", thread="thread", **kwargs):
    async with own_conversation(store.db_path, thread, wait=False) as ownership:
        return await store.reconcile_effect(key, operator="roman", ownership=ownership, **kwargs)


def _rows(store, table):
    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]


async def test_pending_effects_is_the_operator_queue(store):
    turn = await _dispatched(store)
    done = await store.begin_turn("other", "hi")
    claim = await store.begin_external_effect(key="ok-1", turn_id=done, tool="send_payment", args={"n": 1})
    await store.finish_external_effect(key="ok-1", token=claim.token, turn_id=done, tool="send_payment", result="ok")

    pending = await store.pending_effects()
    assert [(p["idempotency_key"], p["turn_id"], p["thread_id"], p["turn_status"], p["args"]) for p in pending] == [
        ("pay-1", turn, "thread", "running", ARGS)
    ]


async def test_happened_becomes_the_recorded_result_and_unblocks_the_turn(store):
    turn = await _dispatched(store)
    with pytest.raises(EffectUncertainError):
        await store.assert_turn_effects_settled(turn)

    done = await _reconcile(store, outcome="happened", result="payment 4411 settled", note="bank statement")

    assert done["turn_status"] == "running" and done["outcome"] == "happened"
    await store.assert_turn_effects_settled(turn)
    recorded = await store.get_external_effect("pay-1")
    assert recorded == "[RECONCILED BY OPERATOR] payment 4411 settled"
    # A continuation reuses the verdict instead of paying again.
    claim = await store.begin_external_effect(
        key="pay-1", turn_id=turn, tool="send_payment", args=ARGS, tool_call_id="c1"
    )
    assert claim.result == recorded and not claim.token
    assert await store.pending_effects() == []
    verdict = (await store.effect_reconciliations(turn))[0]
    assert (verdict["outcome"], verdict["operator"], verdict["note"]) == ("happened", "roman", "bank statement")


async def test_did_not_happen_releases_the_intent_but_keeps_the_evidence(store):
    turn = await _dispatched(store)

    done = await _reconcile(store, outcome="did_not_happen", note="no transaction at the bank")

    assert done["turn_status"] == "running"
    await store.assert_turn_effects_settled(turn)
    assert await store.get_external_effect("pay-1") is None
    archived = _rows(store, "effect_reconciliations")[0]
    assert archived["args_json"] == '{"amount":100,"to":"bank"}' and archived["tool_call_id"] == "c1"
    assert archived["outcome"] == "did_not_happen" and archived["result"] == ""
    # The call may now be dispatched again, through the ordinary intent guard.
    claim = await store.begin_external_effect(
        key="pay-1", turn_id=turn, tool="send_payment", args=ARGS, tool_call_id="c1"
    )
    assert claim.token and claim.result is None


async def test_unknown_fails_an_active_turn_and_never_marks_it_done(store):
    turn = await _dispatched(store)

    done = await _reconcile(store, outcome="unknown", note="provider has no lookup")

    assert done["turn_status"] == "failed"
    outcome = await store.get_turn_outcome(turn)
    assert outcome.status == "failed"
    assert await store.pending_effects() == []
    with pytest.raises(DurableStateError):
        await store.begin_external_effect(key="pay-2", turn_id=turn, tool="send_payment", args=ARGS)


async def test_an_already_failed_turn_is_not_revived_by_a_verdict(store):
    turn = await _dispatched(store)
    await store.fail_turn(turn, "unresolved external effect; reconciliation required")

    done = await _reconcile(store, outcome="happened", result="payment 4411 settled")

    assert done["turn_status"] == "failed"
    assert (await store.get_turn_outcome(turn)).status == "failed"
    assert await store.pending_effects() == []


async def test_a_released_intent_no_longer_blocks_the_same_operation_elsewhere(store):
    await _dispatched(store)
    other = await store.begin_turn("thread-2", "pay the bank")
    with pytest.raises(EffectUncertainError):
        await store.begin_external_effect(key="pay-9", turn_id=other, tool="send_payment", args=ARGS)

    await _reconcile(store, outcome="did_not_happen")

    assert (await store.begin_external_effect(key="pay-9", turn_id=other, tool="send_payment", args=ARGS)).token


async def test_a_live_executor_cannot_be_reconciled_underneath(store):
    await _dispatched(store)
    async with own_conversation(store.db_path, "thread"):
        with pytest.raises(TurnBusyError):
            await _reconcile(store, outcome="unknown")
    assert len(await store.pending_effects()) == 1 and _rows(store, "effect_reconciliations") == []


async def test_ownership_of_another_conversation_is_not_enough(store):
    await _dispatched(store)
    async with own_conversation(store.db_path, "someone-else", wait=False) as ownership:
        with pytest.raises(Exception) as refused:
            await store.reconcile_effect("pay-1", outcome="unknown", operator="roman", ownership=ownership)
    assert not isinstance(refused.value, AssertionError)
    assert len(await store.pending_effects()) == 1 and _rows(store, "effect_reconciliations") == []


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"outcome": "retry"}, ValueError),
        ({"outcome": "happened"}, ValueError),
        ({"outcome": "happened", "result": "   "}, ValueError),
    ],
)
async def test_invalid_verdicts_change_nothing(store, kwargs, error):
    await _dispatched(store)
    with pytest.raises(error):
        await _reconcile(store, **kwargs)
    assert len(await store.pending_effects()) == 1 and _rows(store, "effect_reconciliations") == []


async def test_operator_is_mandatory_and_unknown_or_settled_keys_are_refused(store):
    turn = await _dispatched(store)
    async with own_conversation(store.db_path, "thread", wait=False) as ownership:
        with pytest.raises(ValueError):
            await store.reconcile_effect("pay-1", outcome="unknown", operator=" ", ownership=ownership)
        with pytest.raises(DurableStateError):
            await store.reconcile_effect("missing", outcome="unknown", operator="roman", ownership=ownership)
    await _reconcile(store, outcome="happened", result="settled")
    with pytest.raises(DurableStateError):
        await _reconcile(store, outcome="did_not_happen")
    assert len(await store.effect_reconciliations(turn)) == 1
    assert await store.get_external_effect("pay-1") == "[RECONCILED BY OPERATOR] settled"


async def test_a_reconciled_turn_can_finish_normally(store):
    turn = await _dispatched(store)
    await _reconcile(store, outcome="happened", result="settled")
    await store.finalize_turn(thread_id="thread", messages=[AIMessage(content="paid")], turn_id=turn, content="paid")
    assert (await store.get_turn_outcome(turn)).status == "completed"


async def test_migration_is_additive_and_idempotent(tmp_path):
    path = str(tmp_path / "old.db")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE effect_intents (idempotency_key TEXT PRIMARY KEY, status TEXT)")
        db.execute("INSERT INTO effect_intents VALUES ('old', 'pending')")
    async with aiosqlite.connect(path) as db:
        await migrate(db)
        await migrate(db)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM effect_intents").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM effect_reconciliations").fetchone()[0] == 0


def test_cli_lists_without_secrets_and_records_a_verdict(store, monkeypatch, capsys):
    import asyncio

    import kronos.cli as cli

    async def seed():
        turn = await store.begin_turn("thread", "pay the bank")
        await store.begin_external_effect(
            key="pay-1", turn_id=turn, tool="send_payment", args={"to": "bank", "api_key": "sk-live-secret-value-123"}
        )
        return turn

    turn = asyncio.run(seed())
    monkeypatch.setattr(cli, "_session_store", lambda: store)

    assert cli.main(["turns", "effects"]) == 0
    listing = capsys.readouterr().out
    assert "pay-1" in listing and "send_payment" in listing and "sk-live-secret-value-123" not in listing

    assert cli.main(["turns", "reconcile", "pay-1", "--outcome", "happened"]) == 1
    assert "Not recorded" in capsys.readouterr().out
    assert cli.main(["turns", "reconcile", "nope", "--outcome", "unknown"]) == 1
    capsys.readouterr()

    assert cli.main(["turns", "reconcile", "pay-1", "--outcome", "did-not-happen", "--operator", "roman"]) == 0
    out = capsys.readouterr().out
    assert "did_not_happen" in out and f"kaos turns resume {turn}" in out
    assert cli.main(["turns", "effects"]) == 0
    assert "No unresolved external effects" in capsys.readouterr().out
