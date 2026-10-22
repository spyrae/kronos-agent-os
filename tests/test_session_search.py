import sqlite3
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from kronos.config import settings
from kronos.session import SessionStore


@pytest.fixture
def isolated_session_search(tmp_path, monkeypatch):
    swarm_path = tmp_path / "swarm.db"
    db_dir = tmp_path / "agent"
    db_dir.mkdir()
    session_path = db_dir / "session.db"

    monkeypatch.setattr(settings, "swarm_db_path", str(swarm_path))
    monkeypatch.setattr(settings, "db_dir", str(db_dir))
    monkeypatch.setattr(settings, "db_path", str(session_path))

    from kronos import db as _db

    _db._instances.clear()
    import kronos.swarm_store as ss

    ss._singleton = None

    from kronos.swarm_store import get_swarm

    return session_path, get_swarm()


@pytest.mark.asyncio
async def test_session_save_indexes_messages_without_duplicates(isolated_session_search):
    session_path, swarm = isolated_session_search
    store = SessionStore(str(session_path), agent_name="kronos")
    messages = [
        HumanMessage(content="We discussed Kimi migration details"),
        AIMessage(content="Decision: use the Kimi router"),
    ]

    await store.save("thread-1", messages, user_id="alice")
    await store.save("thread-1", messages, user_id="alice")

    results = swarm.search_sessions(
        query="migration",
        user_id="alice",
        agent_name="kronos",
        days=30,
        limit=10,
    )
    assert len(results) == 1
    assert results[0]["thread_id"] == "thread-1"
    assert results[0]["role"] == "user"


@pytest.mark.asyncio
async def test_backfill_existing_sessions_is_idempotent(isolated_session_search):
    session_path, swarm = isolated_session_search
    writer = SessionStore(str(session_path))
    messages = [
        HumanMessage(content="Arbitration bug notes"),
        AIMessage(content="Fix claim winner ordering"),
    ]
    await writer.save("thread-2", messages)

    store = SessionStore(str(session_path), agent_name="kronos")
    assert await store.backfill_swarm_fts() == 2
    assert await store.backfill_swarm_fts() == 0

    # Stored history has no owner: the rows exist for the operator, not for any user.
    assert swarm.search_sessions(query="arbitration", user_id="alice", days=30, limit=10) == []
    rows = swarm._db.read("SELECT agent_name, user_id FROM session_messages")
    assert [(r["agent_name"], r["user_id"]) for r in rows] == [("kronos", None), ("kronos", None)]


def test_session_search_filters_by_agent_and_days(isolated_session_search):
    _, swarm = isolated_session_search
    swarm.index_session_message(
        agent_name="kronos",
        thread_id="recent",
        role="user",
        content="Kimi migration discussion",
        user_id="alice",
    )
    swarm.index_session_message(
        agent_name="nexus",
        thread_id="other-agent",
        role="user",
        content="Kimi migration discussion from another agent",
        user_id="alice",
    )
    swarm.index_session_message(
        agent_name="kronos",
        thread_id="old",
        role="user",
        content="Legacy launch discussion",
        user_id="alice",
    )
    ancient = time.time() - 40 * 86400
    swarm._db.write(
        "UPDATE session_messages SET created_at = ? WHERE thread_id = ?",
        (ancient, "old"),
    )

    kronos_results = swarm.search_sessions(
        query="Kimi",
        user_id="alice",
        agent_name="kronos",
        days=30,
        limit=10,
    )
    assert [r["thread_id"] for r in kronos_results] == ["recent"]

    assert swarm.search_sessions(query="Legacy", user_id="alice", days=30, limit=10) == []
    assert swarm.search_sessions(query="Legacy", user_id="alice", days=90, limit=10)[0]["thread_id"] == "old"


def test_session_search_falls_back_to_like_when_fts_fails(isolated_session_search, monkeypatch):
    _, swarm = isolated_session_search
    swarm.index_session_message(
        agent_name="kronos",
        thread_id="fallback",
        role="assistant",
        content="Fallback search can still find session notes",
        user_id="alice",
    )
    swarm.index_session_message(
        agent_name="kronos",
        thread_id="foreign",
        role="assistant",
        content="Fallback search must not find another user's notes",
        user_id="bob",
    )

    original_read = swarm._db.read

    def flaky_read(sql, params=()):
        if "session_messages_fts" in sql:
            raise RuntimeError("fts unavailable")
        return original_read(sql, params)

    monkeypatch.setattr(swarm._db, "read", flaky_read)

    results = swarm.search_sessions(query="Fallback search", user_id="alice", days=30, limit=10)
    assert [r["thread_id"] for r in results] == ["fallback"]


# --- A02: session search must never cross the user boundary -----------------


def _seed_two_users(swarm):
    swarm.index_session_message(
        agent_name="kronos", thread_id="100", role="user", content="alice salary negotiation", user_id="alice"
    )
    swarm.index_session_message(
        agent_name="nexus", thread_id="200", role="user", content="bob salary diagnosis", user_id="bob"
    )
    swarm.index_session_message(agent_name="kronos", thread_id="300", role="user", content="legacy salary note")


def test_search_returns_only_the_requesting_users_rows(isolated_session_search):
    _, swarm = isolated_session_search
    _seed_two_users(swarm)

    assert [r["thread_id"] for r in swarm.search_sessions(query="salary", user_id="alice")] == ["100"]
    assert [r["thread_id"] for r in swarm.search_sessions(query="salary", user_id="bob")] == ["200"]
    # The agent filter narrows the owner's rows; it never widens to another owner.
    assert swarm.search_sessions(query="salary", user_id="alice", agent_name="nexus") == []


@pytest.mark.parametrize("owner", ["", "   ", "mallory", "%", "alice' OR '1'='1", "NULL"])
def test_unknown_or_hostile_owner_matches_nothing(isolated_session_search, owner):
    _, swarm = isolated_session_search
    _seed_two_users(swarm)
    assert swarm.search_sessions(query="salary", user_id=owner) == []


def test_owner_is_mandatory_for_search(isolated_session_search):
    _, swarm = isolated_session_search
    with pytest.raises(TypeError):
        swarm.search_sessions(query="salary")


def test_reindexing_does_not_hand_an_existing_row_to_another_user(isolated_session_search):
    _, swarm = isolated_session_search
    first = swarm.index_session_message(
        agent_name="kronos", thread_id="100", role="user", content="alice private plan", user_id="alice"
    )
    second = swarm.index_session_message(
        agent_name="kronos", thread_id="100", role="user", content="alice private plan", user_id="bob"
    )
    assert (first, second) == (True, False)
    assert swarm.search_sessions(query="private", user_id="bob") == []
    assert len(swarm.search_sessions(query="private", user_id="alice")) == 1


def test_legacy_schema_is_migrated_and_old_rows_stay_ownerless(tmp_path, monkeypatch):
    swarm_path = tmp_path / "swarm.db"
    with sqlite3.connect(swarm_path) as db:
        db.executescript(
            """
            CREATE TABLE session_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, agent_name TEXT NOT NULL, thread_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL, fingerprint TEXT
            );
            CREATE VIRTUAL TABLE session_messages_fts
                USING fts5(content, tokenize='unicode61', content='session_messages', content_rowid='id');
            CREATE TRIGGER session_messages_fts_ai AFTER INSERT ON session_messages BEGIN
                INSERT INTO session_messages_fts(rowid, content) VALUES (new.id, new.content);
            END;
            """
        )
        db.execute(
            "INSERT INTO session_messages (agent_name, thread_id, role, content, created_at, fingerprint)"
            " VALUES ('kronos', '100', 'user', 'old secret roadmap', ?, 'fp-old')",
            (time.time(),),
        )
    monkeypatch.setattr(settings, "swarm_db_path", str(swarm_path))
    import kronos.swarm_store as ss
    from kronos import db as _db

    _db._instances.clear()
    ss._singleton = None
    swarm = ss.get_swarm()

    # The first user to ask does not inherit a row whose owner was never recorded.
    assert swarm.search_sessions(query="roadmap", user_id="100") == []
    assert swarm.search_sessions(query="roadmap", user_id="alice") == []
    assert swarm._db.read("SELECT content, user_id FROM session_messages")[0]["user_id"] is None

    swarm.index_session_message(
        agent_name="kronos", thread_id="100", role="user", content="new roadmap", user_id="alice"
    )
    assert [r["content"] for r in swarm.search_sessions(query="roadmap", user_id="alice")] == ["new roadmap"]
    _db._instances.clear()
    ss._singleton = None


# --- the tool takes its owner from the runtime, not from the model ----------


def test_tool_refuses_without_a_bound_owner(isolated_session_search):
    from kronos.tools.session_search import session_search

    _, swarm = isolated_session_search
    _seed_two_users(swarm)
    reply = session_search.invoke({"query": "salary", "summarize": False})
    assert "недоступен" in reply and "salary" not in reply


def test_tool_has_no_argument_that_selects_a_user():
    from kronos.tools.session_search import session_search

    assert not {"user_id", "user", "owner"} & set(session_search.args)


async def test_tool_searches_only_the_bound_owner(isolated_session_search):
    from kronos.memory.scope import data_owner_scope
    from kronos.tools.session_search import session_search

    _, swarm = isolated_session_search
    _seed_two_users(swarm)

    with data_owner_scope("bob"):
        sync_reply = session_search.invoke({"query": "salary", "summarize": False})
        async_reply = await session_search.ainvoke({"query": "salary", "summarize": False})
    for reply in (sync_reply, async_reply):
        assert "diagnosis" in reply
        assert "negotiation" not in reply and "legacy" not in reply


async def test_group_thread_rows_belong_to_whoever_took_the_turn(isolated_session_search):
    """Two users in one thread: neither can search the other user's separate DM."""
    session_path, swarm = isolated_session_search
    store = SessionStore(str(session_path), agent_name="kronos")
    await store.save("dm-alice", [HumanMessage(content="alice passport renewal")], user_id="alice")
    await store.save("group-1", [HumanMessage(content="shared launch checklist")], user_id="alice")
    await store.save(
        "group-1",
        [HumanMessage(content="shared launch checklist"), HumanMessage(content="bob adds launch budget")],
        user_id="bob",
    )

    assert swarm.search_sessions(query="passport", user_id="bob") == []
    assert [r["content"] for r in swarm.search_sessions(query="launch", user_id="bob")] == ["bob adds launch budget"]
    assert [r["content"] for r in swarm.search_sessions(query="launch", user_id="alice")] == [
        "shared launch checklist"
    ]


async def test_finalize_turn_indexes_under_the_turn_owner(isolated_session_search):
    session_path, swarm = isolated_session_search
    store = SessionStore(str(session_path), agent_name="kronos")
    turn_id = await store.begin_turn("100", "quarterly forecast please", user_id="alice")
    await store.finalize_turn(
        thread_id="100",
        messages=[HumanMessage(content="quarterly forecast please"), AIMessage(content="forecast is ready")],
        turn_id=turn_id,
        content="forecast is ready",
    )
    assert len(swarm.search_sessions(query="forecast", user_id="alice")) == 2
    assert swarm.search_sessions(query="forecast", user_id="bob") == []


# --- operator assignment of ownerless rows -----------------------------------


def test_operator_assigns_one_thread_and_never_takes_over_an_owned_row(isolated_session_search):
    _, swarm = isolated_session_search
    swarm.index_session_message(agent_name="kronos", thread_id="100", role="user", content="old alpha note")
    swarm.index_session_message(agent_name="kronos", thread_id="200", role="user", content="old beta note")
    swarm.index_session_message(agent_name="nexus", thread_id="100", role="user", content="other agent alpha note")
    swarm.index_session_message(
        agent_name="kronos", thread_id="100", role="user", content="bob alpha note", user_id="bob"
    )

    assert swarm.ownerless_session_threads(agent_name="kronos") == [
        {"thread_id": "100", "messages": 1},
        {"thread_id": "200", "messages": 1},
    ]
    with pytest.raises(ValueError):
        swarm.assign_session_owner(agent_name="kronos", thread_id="100", user_id=" ")

    assert swarm.assign_session_owner(agent_name="kronos", thread_id="100", user_id="alice") == 1
    assert swarm.assign_session_owner(agent_name="kronos", thread_id="100", user_id="alice") == 0

    assert [r["content"] for r in swarm.search_sessions(query="alpha", user_id="alice")] == ["old alpha note"]
    assert [r["content"] for r in swarm.search_sessions(query="alpha", user_id="bob")] == ["bob alpha note"]
    assert swarm.search_sessions(query="beta", user_id="alice") == []
    assert swarm.ownerless_session_threads(agent_name="nexus") == [{"thread_id": "100", "messages": 1}]


def test_cli_lists_counts_and_assigns_only_on_explicit_request(isolated_session_search, monkeypatch, capsys):
    import kronos.cli as cli
    from kronos.memory import knowledge_graph

    _, swarm = isolated_session_search
    monkeypatch.setattr(settings, "agent_name", "kronos")
    monkeypatch.setattr(knowledge_graph, "_schema_initialized", False)
    swarm.index_session_message(agent_name="kronos", thread_id="100", role="user", content="old private note")

    assert cli.main(["sessions", "owners"]) == 0
    listing = capsys.readouterr().out
    assert "thread 100 — 1 message(s)" in listing and "private" not in listing

    assert cli.main(["sessions", "assign-owner", "--user-id", "alice"]) == 2
    assert swarm.search_sessions(query="private", user_id="alice") == []

    assert cli.main(["sessions", "assign-owner", "--user-id", "alice", "--thread", "100", "--graph"]) == 0
    assert "thread 100: 1 message(s) assigned" in capsys.readouterr().out
    assert len(swarm.search_sessions(query="private", user_id="alice")) == 1


# --- trimming must not re-attribute history -----------------------------------


async def test_trimmed_history_is_not_reindexed_under_the_next_speaker(isolated_session_search):
    """Alice fills a group thread past the history limit, then Bob speaks once."""
    from kronos.session import MAX_HISTORY

    session_path, swarm = isolated_session_search
    store = SessionStore(str(session_path), agent_name="kronos")
    history = []
    for index in range(MAX_HISTORY + 4):
        history = [*history, HumanMessage(content=f"alice secret number {index}")]
        await store.save("group-1", history, user_id="alice")
    history = [*history, HumanMessage(content="bob asks about lunch")]
    await store.save("group-1", history, user_id="bob")

    assert swarm.search_sessions(query="alice secret", user_id="bob") == []
    assert [r["content"] for r in swarm.search_sessions(query="lunch", user_id="bob")] == ["bob asks about lunch"]
    owners = {r["user_id"] for r in swarm._db.read("SELECT user_id FROM session_messages WHERE content LIKE 'alice%'")}
    assert owners == {"alice"}


async def test_ownerless_legacy_rows_are_not_adopted_by_a_later_save(isolated_session_search):
    session_path, swarm = isolated_session_search
    swarm.index_session_message(agent_name="kronos", thread_id="100", role="user", content="old private plan")

    store = SessionStore(str(session_path), agent_name="kronos")
    await store.save("100", [HumanMessage(content="old private plan"), HumanMessage(content="new question")], user_id="mallory")

    assert swarm.search_sessions(query="private", user_id="mallory") == []
    assert [r["content"] for r in swarm.search_sessions(query="question", user_id="mallory")] == ["new question"]
