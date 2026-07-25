"""Turn inspection, forking and retention (moat phases 10.3 and 10.5)."""

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from kronos.cli import build_parser, main
from kronos.config import settings
from kronos.session import SessionStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "agent_name", "turns-test")
    import kronos.db as _db

    _db._instances.clear()
    yield SessionStore(str(tmp_path / "session.db"), agent_name="turns-test")
    _db._instances.clear()


async def _turn_with_journal(store, *, thread_id="chat-7", text="покажи расходы"):
    turn_id = await store.begin_turn(thread_id, text)
    await store.append_turn_messages(
        turn_id=turn_id,
        thread_id=thread_id,
        messages=[
            AIMessage(content="", tool_calls=[{"name": "get_expenses", "args": {"scope": "day"}, "id": "c1"}]),
            ToolMessage(content="12.5 USD", tool_call_id="c1"),
            AIMessage(content="За день 12.5 USD."),
        ],
    )
    await store.save_tool_result(turn_id=turn_id, tool_call_id="c1", content="12.5 USD")
    await store.record_external_effect(key="k1", turn_id=turn_id, tool="send_message", result="sent")
    return turn_id


def test_parser_exposes_turn_commands():
    parser = build_parser()

    assert parser.parse_args(["turns", "list", "--status", "running"]).status == "running"
    assert parser.parse_args(["turns", "show", "t1"]).turn_id == "t1"
    forked = parser.parse_args(["turns", "fork", "t1", "--at", "2", "--thread", "new"])
    assert forked.at_seq == 2 and forked.thread == "new"
    assert parser.parse_args(["turns", "resume", "t1"]).turns_command == "resume"


@pytest.mark.asyncio
async def test_list_and_filter_turns(store):
    running = await _turn_with_journal(store)
    await store.finish_turn(running)
    pending = await store.begin_turn("chat-9", "второй запрос")

    all_turns = await store.list_turns(limit=10)
    only_running = await store.list_turns(status="running")
    by_thread = await store.list_turns(thread_id="chat-9")

    assert {turn["turn_id"] for turn in all_turns} == {running, pending}
    assert [turn["turn_id"] for turn in only_running] == [pending]
    assert [turn["turn_id"] for turn in by_thread] == [pending]


@pytest.mark.asyncio
async def test_turn_detail_includes_journal_results_and_effects(store):
    turn_id = await _turn_with_journal(store)

    detail = await store.get_turn_detail(turn_id)

    assert detail["thread_id"] == "chat-7"
    assert [row["seq"] for row in detail["journal"]] == [1, 2, 3]
    assert detail["journal"][0]["message"]["tool_calls"][0]["name"] == "get_expenses"
    assert detail["tool_results"][0]["tool_call_id"] == "c1"
    assert detail["effects"][0]["tool"] == "send_message"
    assert await store.get_turn_detail("nope") is None


@pytest.mark.asyncio
async def test_fork_copies_prefix_and_leaves_the_original(store):
    turn_id = await _turn_with_journal(store)

    forked = await store.fork_turn(turn_id, at_seq=2, new_thread_id="fork-thread")

    assert forked["thread_id"] == "fork-thread"
    assert forked["messages"] == 3  # input + two journal entries up to seq 2
    original = await store.get_turn_detail(turn_id)
    assert len(original["journal"]) == 3, "forking must not touch the source turn"
    assert len(await store.load("fork-thread")) == 3


@pytest.mark.asyncio
async def test_fork_default_thread_name_is_derived(store):
    turn_id = await _turn_with_journal(store)

    forked = await store.fork_turn(turn_id)

    assert forked["thread_id"].startswith("chat-7:fork-")


@pytest.mark.asyncio
async def test_running_turn_stats_expose_stuck_work(store):
    assert await store.running_turn_stats() == {"running_turns": 0, "oldest_running_age_seconds": None}

    await store.begin_turn("chat-1", "висит")
    stats = await store.running_turn_stats()

    assert stats["running_turns"] == 1
    assert stats["oldest_running_age_seconds"] is not None


@pytest.mark.asyncio
async def test_retention_prunes_finished_turns_only(store):
    finished = await _turn_with_journal(store, text="старый завершённый")
    await store.finish_turn(finished)
    running = await store.begin_turn("chat-1", "ещё в работе")

    # Age both turns past the window.
    async with store._open_db() as db:
        await db.execute("UPDATE active_turns SET started_at = datetime('now', '-60 days')")
        await db.execute(
            "UPDATE active_turns SET completed_at = datetime('now', '-60 days') WHERE turn_id = ?", (finished,)
        )
        await db.commit()

    pruned = await store.prune_turn_history(older_than_days=30)

    assert pruned["turns"] == 1
    # finish_turn already dropped the journal and memoized results, so what
    # retention actually reclaims here is the turn row and its effects.
    assert pruned["journal"] == 0
    assert pruned["effects"] == 1
    assert await store.get_turn_detail(finished) is None
    assert await store.get_turn_detail(running) is not None, "live state must survive retention"


@pytest.mark.asyncio
async def test_retention_keeps_recent_turns(store):
    finished = await _turn_with_journal(store)
    await store.finish_turn(finished)

    pruned = await store.prune_turn_history(older_than_days=30)

    assert pruned["turns"] == 0
    assert await store.get_turn_detail(finished) is not None


@pytest.mark.asyncio
async def test_retention_job_reads_the_policy(store, tmp_path, monkeypatch):
    import yaml

    from kronos.cron.turn_retention import run_turn_retention
    from kronos.policy import ENV_POLICY_FILE, reset_policy

    finished = await _turn_with_journal(store)
    await store.finish_turn(finished)
    async with store._open_db() as db:
        await db.execute("UPDATE active_turns SET completed_at = datetime('now', '-10 days')")
        await db.commit()

    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump({"retention": {"turn_journal_days": 7}}), encoding="utf-8")
    monkeypatch.setenv(ENV_POLICY_FILE, str(path))
    reset_policy()

    await run_turn_retention()

    assert await store.get_turn_detail(finished) is None
    reset_policy()


def test_cli_list_and_show(store, capsys):
    import asyncio

    turn_id = asyncio.run(_turn_with_journal(store))

    assert main(["turns", "list"]) == 0
    listed = capsys.readouterr().out
    assert turn_id in listed
    assert "kaos turns resume" in listed  # the turn is in flight

    assert main(["turns", "show", turn_id]) == 0
    shown = capsys.readouterr().out
    assert "get_expenses" in shown
    assert "recorded external effects" in shown
    assert "send_message" in shown


def test_cli_show_unknown_turn(store, capsys):
    assert main(["turns", "show", "missing"]) == 1
    assert "Turn not found" in capsys.readouterr().out


def test_cli_fork(store, capsys):
    import asyncio

    turn_id = asyncio.run(_turn_with_journal(store))

    assert main(["turns", "fork", turn_id, "--at", "1", "--thread", "experiment"]) == 0
    out = capsys.readouterr().out

    assert "experiment" in out
    assert "original turn is untouched" in out


def test_cli_resume_refuses_a_finished_turn(store, capsys, monkeypatch):
    import asyncio

    monkeypatch.setattr("kronos.cli._runtime_llm_configured", lambda: True)
    turn_id = asyncio.run(_turn_with_journal(store))
    asyncio.run(store.finish_turn(turn_id))

    assert main(["turns", "resume", turn_id]) == 1
    assert "only in-flight turns can be resumed" in capsys.readouterr().out


def test_cli_list_on_empty_store(store, capsys):
    assert main(["turns", "list"]) == 0
    assert "No durable turns recorded" in capsys.readouterr().out
