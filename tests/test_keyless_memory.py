"""A05: without a DeepSeek key memory still works in keyword mode — and stays per user."""

import logging

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from kronos.config import settings


@pytest.fixture
def keyless(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    monkeypatch.setattr(settings, "agent_name", "kronos")
    import kronos.db as db_module
    import kronos.swarm_store as swarm_module
    from kronos.memory import fts, knowledge_graph, store

    db_module._instances.clear()
    swarm_module._singleton = None
    monkeypatch.setattr(knowledge_graph, "_schema_initialized", False)
    monkeypatch.setattr(fts, "_schema_initialized", False)

    def no_mem0():
        raise AssertionError("Mem0 must not be constructed without a key")

    monkeypatch.setattr(store, "get_memory", no_mem0)
    yield
    db_module._instances.clear()
    swarm_module._singleton = None


def _state(user_id, text, reply="understood"):
    return {"messages": [HumanMessage(content=text), AIMessage(content=reply)], "user_id": user_id, "session_id": "s"}


def _injected(user_id, text):
    from kronos.memory import nodes

    update = nodes.retrieve_memories({"messages": [HumanMessage(content=text)], "user_id": user_id})
    return update["messages"][0].content if update else ""


def test_the_agent_enables_memory_without_a_key(keyless, monkeypatch):
    from kronos.graph import KronosAgent

    agent = object.__new__(KronosAgent)
    agent._init_memory(True)
    assert agent._memory_enabled is True
    agent._init_memory(False)
    assert agent._memory_enabled is False


def test_a_turn_is_remembered_and_recalled_by_keyword_without_any_model(keyless, caplog):
    from kronos.memory import nodes

    with caplog.at_level(logging.ERROR):
        nodes.store_memories_background(_state("alice", "my passport expires in March"))
        recalled = _injected("alice", "passport")

    assert "passport expires in March" in recalled
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], "keyword mode is not an error"


def test_keyword_memory_never_crosses_users(keyless):
    from kronos.memory import knowledge_graph, nodes
    from kronos.swarm_store import get_swarm

    nodes.store_memories_background(_state("alice", "my passport expires in March"))
    nodes.store_memories_background(_state("bob", "my passport is at the embassy"))
    get_swarm().add_shared_fact(user_id="alice", fact="alice passport number ends 77", source_agent="nexus")
    knowledge_graph.add_relation("passport", "document", "Alice", "person", "belongs_to", user_id="alice")

    alice, bob = _injected("alice", "passport"), _injected("bob", "passport")

    assert "expires in March" in alice and "ends 77" in alice and "belongs_to" in alice
    assert "embassy" not in alice
    assert "embassy" in bob
    assert "March" not in bob and "ends 77" not in bob and "belongs_to" not in bob
    assert _injected("mallory", "passport") == ""
    assert _injected("", "passport") == ""


def test_shared_facts_written_by_an_agent_with_a_key_are_readable_without_one(keyless):
    from kronos.swarm_store import get_swarm

    get_swarm().add_shared_fact(user_id="alice", fact="prefers short technical answers", source_agent="nexus")
    assert "prefers short technical answers" in _injected("alice", "technical answers")


def test_without_extraction_nothing_is_mirrored_as_a_shared_fact(keyless):
    from kronos.memory import nodes
    from kronos.swarm_store import get_swarm

    nodes.store_memories_background(_state("alice", "my passport expires in March"))
    assert get_swarm().all_shared_facts(user_id="alice") == []


def test_search_and_add_degrade_quietly(keyless, caplog):
    from kronos.memory import store

    with caplog.at_level(logging.ERROR):
        assert store.add_memories([{"role": "user", "content": "x"}], "alice") == []
        assert store.search_memories("anything", user_id="alice") == []
    assert caplog.records == []
    assert store.extraction_configured() is False


def test_with_a_key_the_vector_path_is_still_used(keyless, monkeypatch):
    from kronos.memory import store

    calls = []

    class Mem0:
        def search(self, query, user_id, limit):
            calls.append(("search", user_id))
            return {"results": []}

        def add(self, messages, user_id, **kwargs):
            calls.append(("add", user_id))
            return {"results": []}

    monkeypatch.setattr(settings, "deepseek_api_key", "key")
    monkeypatch.setattr(store, "get_memory", lambda: Mem0())
    store.search_memories("anything", user_id="alice")
    store.add_memories([{"role": "user", "content": "x"}], "alice")
    assert calls == [("search", "alice"), ("add", "alice")]
