"""F13: a memory reset covers every layer and a delayed writer cannot undo it."""

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi import HTTPException
from langchain_core.messages import AIMessage, HumanMessage

from kronos.config import settings
from kronos.memory import reset as reset_module
from kronos.memory.reset import MemoryResetError, memory_generation, memory_write, reset_memory


class FakeMem0:
    """Stands in for the Mem0 SDK; the real package is not installed locally."""

    def __init__(self):
        self.rows: list[tuple[str, str]] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.fail_delete = False

    def add(self, messages, user_id, **kwargs):
        self.entered.set()
        assert self.release.wait(5)
        fact = f"fact from: {messages[0]['content']}"
        self.rows.append((user_id, fact))
        return {"results": [{"memory": fact}]}

    def search(self, query, user_id, limit=5):
        return {"results": []}

    def delete_all(self, *, user_id):
        if self.fail_delete:
            raise OSError("qdrant locked")
        self.rows = [row for row in self.rows if row[0] != user_id]

    def reset(self):
        if self.fail_delete:
            raise OSError("qdrant locked")
        self.rows = []


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "mem0_qdrant_path", str(tmp_path / "qdrant"))
    monkeypatch.setattr(settings, "agent_name", "kronos")
    import kronos.db as db_module
    import kronos.swarm_store as swarm_module
    from kronos.memory import fts, knowledge_graph, store

    db_module._instances.clear()
    swarm_module._singleton = None
    monkeypatch.setattr(knowledge_graph, "_schema_initialized", False)
    monkeypatch.setattr(fts, "_schema_initialized", False)
    monkeypatch.setattr(reset_module, "_gate", reset_module._WriterGate())
    mem0 = FakeMem0()
    monkeypatch.setattr(store, "get_memory", lambda: mem0)
    yield mem0
    db_module._instances.clear()
    swarm_module._singleton = None


def _state(user_id, text, *, generation=None):
    state = {
        "messages": [HumanMessage(content=text), AIMessage(content="understood")],
        "user_id": user_id,
        "session_id": "s",
    }
    if generation is not None:
        state["memory_generation"] = generation
    return state


def _layers(mem0, user_id):
    from kronos.memory import fts, knowledge_graph
    from kronos.swarm_store import get_swarm

    return {
        "mem0": [fact for owner, fact in mem0.rows if owner == user_id],
        "fts": fts.get_stats(user_id)["total_facts"],
        "shared": get_swarm().all_shared_facts(user_id=user_id),
        "graph": knowledge_graph.search_entities("", user_id=user_id),
        "sessions": get_swarm().search_sessions(query="salary", user_id=user_id),
    }


EMPTY = {"mem0": [], "fts": 0, "shared": [], "graph": [], "sessions": []}


async def _seed(mem0, user_id):
    from kronos.memory import knowledge_graph, nodes
    from kronos.session import SessionStore

    nodes.store_memories_background(_state(user_id, f"{user_id} salary is private"))
    knowledge_graph.add_relation("Acme", "company", user_id, "person", "employs", user_id=user_id)
    store = SessionStore(settings.db_path, agent_name="kronos")
    turn_id = await store.begin_turn(f"dm-{user_id}", "salary talk", user_id=user_id)
    await store.finalize_turn(
        thread_id=f"dm-{user_id}",
        messages=[HumanMessage(content=f"{user_id} salary talk"), AIMessage(content="salary noted")],
        turn_id=turn_id,
        content="salary noted",
    )


# --- coverage of every layer --------------------------------------------------


async def test_user_reset_clears_every_layer_for_that_user_only(env):
    await _seed(env, "alice")
    await _seed(env, "bob")
    before_bob = _layers(env, "bob")
    assert all(_layers(env, "alice").values()) and all(before_bob.values())

    report = await reset_memory(set(reset_module.SCOPES), user_id="alice")

    assert report["ok"] and report["errors"] == {} and report["agent"] == "kronos"
    assert set(report["layers"]) == set(reset_module.SCOPES)
    assert _layers(env, "alice") == EMPTY
    assert _layers(env, "bob") == before_bob
    from kronos.session import SessionStore

    store = SessionStore(settings.db_path)
    assert await store.load("dm-alice") == [] and await store.load("dm-bob")


async def test_global_reset_clears_everyone_including_ownerless_rows(env):
    from kronos.swarm_store import get_swarm

    await _seed(env, "alice")
    await _seed(env, "bob")
    get_swarm().index_session_message(agent_name="kronos", thread_id="old", role="user", content="legacy salary")
    get_swarm().index_session_message(
        agent_name="nexus", thread_id="n", role="user", content="nexus salary", user_id="alice"
    )

    report = await reset_memory(set(reset_module.SCOPES))

    assert report["ok"]
    assert _layers(env, "bob") == EMPTY and env.rows == []
    # Another agent's rows are that agent's to reset.
    assert get_swarm()._db.read("SELECT agent_name FROM session_messages")[0]["agent_name"] == "nexus"
    assert len(get_swarm()._db.read("SELECT 1 FROM session_messages")) == 1


async def test_only_the_requested_scope_is_touched(env):
    await _seed(env, "alice")
    report = await reset_memory({"knowledge_graph"}, user_id="alice")
    after = _layers(env, "alice")
    assert report["layers"] == {"knowledge_graph": {"entities": 2, "relations": 1}}
    assert after["graph"] == [] and after["fts"] and after["shared"] and after["mem0"] and after["sessions"]


async def test_unknown_scope_is_rejected_before_anything_is_deleted(env):
    await _seed(env, "alice")
    with pytest.raises(ValueError):
        await reset_memory({"facts", "everything"}, user_id="alice")
    assert all(_layers(env, "alice").values())


# --- honesty about partial failure -------------------------------------------


async def test_a_failed_layer_is_reported_and_the_other_layers_still_run(env):
    await _seed(env, "alice")
    env.fail_delete = True

    report = await reset_memory(set(reset_module.SCOPES), user_id="alice")

    assert report["ok"] is False and report["errors"] == {"facts": "OSError"}
    assert "facts" not in report["layers"]
    assert _layers(env, "alice")["shared"] == [] and _layers(env, "alice")["graph"] == []
    assert _layers(env, "alice")["mem0"], "the fake refused to delete, so the report must not claim success"


async def test_unconfigured_mem0_is_absent_only_when_no_store_exists_on_disk(env, monkeypatch):
    from kronos.memory import store

    def unconfigured():
        raise RuntimeError("DeepSeek memory extraction is not configured")

    monkeypatch.setattr(store, "get_memory", unconfigured)
    report = await reset_memory({"facts"}, user_id="alice")
    assert report["ok"] and report["layers"]["facts"]["mem0"] == "absent"

    Path(settings.mem0_qdrant_path).mkdir()
    report = await reset_memory({"facts"}, user_id="alice")
    assert report["ok"] is False and report["errors"] == {"facts": "RuntimeError"}


async def test_dashboard_reset_fails_loudly_and_accepts_a_user_scope(env):
    from dashboard.api import memory as api

    await _seed(env, "alice")
    await _seed(env, "bob")

    with pytest.raises(HTTPException) as refused:
        await api.reset_memory(api.ResetMemory(scope="all", confirm=False))
    assert refused.value.status_code == 400
    with pytest.raises(HTTPException) as bad_scope:
        await api.reset_memory(api.ResetMemory(scope="everything", confirm=True))
    assert bad_scope.value.status_code == 400

    done = await api.reset_memory(api.ResetMemory(scope="all", confirm=True, user_id="alice"))
    assert done["ok"] and done["user_id"] == "alice" and done["deleted"] > 0
    assert _layers(env, "alice") == EMPTY and all(_layers(env, "bob").values())

    env.fail_delete = True
    with pytest.raises(HTTPException) as failed:
        await api.reset_memory(api.ResetMemory(scope="all", confirm=True))
    assert failed.value.status_code == 500
    assert failed.value.detail["errors"] == {"facts": "OSError"} and failed.value.detail["ok"] is False


# --- the race with background writers ----------------------------------------


def test_generation_changes_for_the_reset_user_and_for_a_global_reset(env):
    from kronos.swarm_store import get_swarm

    alice, bob = memory_generation("alice"), memory_generation("bob")
    get_swarm().advance_memory_generation("alice")
    assert memory_generation("alice") != alice and memory_generation("bob") == bob

    alice, bob = memory_generation("alice"), memory_generation("bob")
    get_swarm().advance_memory_generation()
    assert memory_generation("alice") != alice and memory_generation("bob") != bob

    with pytest.raises(MemoryResetError):
        with memory_write("alice", alice):
            pytest.fail("a write planned before the reset must not run")
    with memory_write("alice", memory_generation("alice")):
        pass


async def test_a_turn_that_started_before_the_reset_writes_nothing_afterwards(env):
    """The writer runs entirely after the reset — the plain resurrection case."""
    from kronos.memory import nodes

    captured = memory_generation("alice")
    await reset_memory(set(reset_module.SCOPES), user_id="alice")

    nodes.store_memories_background(_state("alice", "alice salary is private", generation=captured))

    assert _layers(env, "alice") == EMPTY


async def test_a_writer_caught_mid_extraction_is_wiped_and_cannot_mirror_afterwards(env):
    """The reset arrives while Mem0 is extracting: it waits, deletes, and the
    shared-fact mirror that follows is refused inside its own transaction."""
    from kronos.memory import nodes

    captured = memory_generation("alice")
    env.release.clear()
    writer = asyncio.create_task(
        asyncio.to_thread(
            nodes.store_memories_background, _state("alice", "alice salary is private", generation=captured)
        )
    )
    await asyncio.to_thread(env.entered.wait, 5)

    resetting = asyncio.create_task(reset_memory(set(reset_module.SCOPES), user_id="alice"))
    await asyncio.sleep(0.1)
    assert not resetting.done(), "the reset must wait for the writer already inside the stores"

    env.release.set()
    await writer
    report = await resetting

    assert report["ok"]
    assert _layers(env, "alice") == EMPTY


async def test_other_users_keep_writing_through_someone_elses_reset(env):
    from kronos.memory import nodes

    captured = memory_generation("bob")
    await reset_memory(set(reset_module.SCOPES), user_id="alice")
    nodes.store_memories_background(_state("bob", "bob salary is private", generation=captured))
    layers = _layers(env, "bob")
    assert layers["mem0"] and layers["fts"] and layers["shared"]


def test_shared_fact_insert_checks_the_generation_in_its_own_transaction(env):
    from kronos.swarm_store import get_swarm

    swarm = get_swarm()
    stale = swarm.memory_generation("alice")
    swarm.advance_memory_generation("alice")
    assert swarm.add_shared_fact(user_id="alice", fact="old fact", source_agent="nexus", generation=stale) is False
    assert swarm.add_shared_fact(
        user_id="alice", fact="new fact", source_agent="nexus", generation=swarm.memory_generation("alice")
    )
    assert swarm.all_shared_facts(user_id="alice") == ["new fact"]


async def test_sleep_compute_does_not_rebuild_a_graph_that_was_reset_mid_extraction(env, monkeypatch):
    from kronos.cron import sleep_compute
    from kronos.memory import knowledge_graph
    from kronos.swarm_store import get_swarm

    class Model:
        def invoke(self, messages):
            # The reset lands while the model is still extracting.
            get_swarm().advance_memory_generation("alice")
            return type("R", (), {"content": '{"entities": [{"name": "Merger", "type": "project"}]}'})()

    monkeypatch.setattr(sleep_compute, "get_model", lambda tier: Model())
    assert await sleep_compute._extract_and_build_graph(["alice runs the merger"], "alice") == (0, 0)
    assert knowledge_graph.search_entities("", user_id="alice") == []


async def test_an_unreadable_generation_means_the_turn_is_not_stored(env, monkeypatch):
    from kronos.graph import KronosAgent
    from kronos.memory import nodes

    monkeypatch.setattr(
        "kronos.graph.memory_generation", lambda user_id: (_ for _ in ()).throw(sqlite3.OperationalError())
    )
    marker = KronosAgent._memory_generation("alice")
    nodes.store_memories_background(_state("alice", "alice salary is private", generation=marker))
    assert _layers(env, "alice") == EMPTY


def test_a_waiting_reset_is_not_starved_by_new_writers(env):
    gate = reset_module._WriterGate()
    order: list[str] = []
    first_in, first_out = threading.Event(), threading.Event()

    def first_writer():
        with gate.writing():
            first_in.set()
            first_out.wait(5)
        order.append("writer-1")

    def resetter():
        with gate.resetting():
            order.append("reset")

    def late_writer():
        with gate.writing():
            order.append("writer-2")

    threads = [threading.Thread(target=first_writer)]
    threads[0].start()
    first_in.wait(5)
    threads.append(threading.Thread(target=resetter))
    threads[1].start()
    while not gate._resets_waiting:
        pass
    threads.append(threading.Thread(target=late_writer))
    threads[2].start()
    first_out.set()
    for thread in threads:
        thread.join(5)
    assert order == ["writer-1", "reset", "writer-2"]


# --- deleting a single fact ---------------------------------------------------


class _Mem0WithIds(FakeMem0):
    def get_all(self, *, user_id, limit=100):
        assert limit > 100, "the default page would hide facts past the first hundred"
        return {
            "results": [
                {"id": f"m{i}", "memory": fact} for i, (owner, fact) in enumerate(self.rows) if owner == user_id
            ]
        }

    def delete(self, memory_id):
        if self.fail_delete:
            raise OSError("qdrant locked")
        del self.rows[int(memory_id[1:])]


async def test_deleting_one_fact_removes_it_from_mem0_fts_and_shared(env, monkeypatch):
    from dashboard.api import memory as api
    from kronos.memory import fts, nodes, store
    from kronos.swarm_store import get_swarm

    mem0 = _Mem0WithIds()
    monkeypatch.setattr(store, "get_memory", lambda: mem0)
    nodes.store_memories_background(_state("alice", "alice salary is private"))
    nodes.store_memories_background(_state("bob", "alice salary is private"))
    fact = "fact from: alice salary is private"
    row_id = fts._get_db().read_one("SELECT id FROM memory_facts WHERE user_id = 'alice' AND content = ?", (fact,))[
        "id"
    ]

    mem0.fail_delete = True
    with pytest.raises(HTTPException) as failed:
        await api.delete_memory_record(f"fts:{row_id}")
    assert failed.value.status_code == 500
    assert fts._get_db().read_one("SELECT 1 FROM memory_facts WHERE id = ?", (row_id,)), (
        "still listed, so it can be retried"
    )

    mem0.fail_delete = False
    done = await api.delete_memory_record(f"fts:{row_id}")
    assert done["ok"] and done["deleted"] == 3
    assert ("alice", fact) not in mem0.rows and ("bob", fact) in mem0.rows
    assert get_swarm().all_shared_facts(user_id="alice") == []
    assert get_swarm().all_shared_facts(user_id="bob") == [fact]
    assert (await api.delete_memory_record(f"fts:{row_id}"))["ok"] is False
    with pytest.raises(HTTPException):
        await api.delete_memory_record("fts:1 OR 1=1")
