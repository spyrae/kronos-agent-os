"""A02: one user's knowledge graph is never readable or writable as another's."""

import sqlite3

import pytest
from langchain_core.messages import HumanMessage

from kronos.config import settings


@pytest.fixture
def kg(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    import kronos.db as db_module
    import kronos.swarm_store as swarm_module
    from kronos.memory import fts, knowledge_graph

    db_module._instances.clear()
    swarm_module._singleton = None
    monkeypatch.setattr(knowledge_graph, "_schema_initialized", False)
    monkeypatch.setattr(fts, "_schema_initialized", False)
    yield knowledge_graph
    db_module._instances.clear()
    swarm_module._singleton = None


def _seed(kg):
    kg.add_relation("Acme", "company", "Merger", "project", "owns", user_id="alice")
    kg.add_relation("Acme", "company", "Layoffs", "project", "owns", user_id="bob")


def test_each_owner_reads_only_their_own_graph(kg):
    _seed(kg)

    assert kg.get_graph_context("Acme", user_id="alice") == "Acme (company): owns→Merger(project)"
    assert kg.get_graph_context("Acme", user_id="bob") == "Acme (company): owns→Layoffs(project)"
    assert [e["name"] for e in kg.search_entities("", user_id="alice")] and all(
        e["name"] != "Layoffs" for e in kg.search_entities("", user_id="alice")
    )
    assert kg.get_entity("Layoffs", user_id="alice") is None
    assert kg.get_connections("Layoffs", user_id="alice") == []


@pytest.mark.parametrize("owner", ["", "  ", "mallory", "%", "alice' OR '1'='1"])
def test_unknown_or_hostile_owner_reads_nothing(kg, owner):
    _seed(kg)
    assert kg.get_graph_context("Acme", user_id=owner) == ""
    assert kg.search_entities("", user_id=owner) == []
    assert kg.get_entity("Acme", user_id=owner) is None
    assert kg.get_connections("Acme", user_id=owner) == []


@pytest.mark.parametrize("call", ["add_entity", "add_relation", "get_entity", "search_entities", "get_graph_context"])
def test_owner_is_mandatory(kg, call):
    args = {
        "add_entity": ("Acme", "company"),
        "add_relation": ("Acme", "company", "Merger", "project", "owns"),
        "get_entity": ("Acme",),
        "search_entities": ("Acme",),
        "get_graph_context": ("Acme",),
    }[call]
    with pytest.raises(TypeError):
        getattr(kg, call)(*args)


@pytest.mark.parametrize("owner", ["", "   ", None])
def test_writes_without_an_owner_are_refused(kg, owner):
    with pytest.raises(ValueError):
        kg.add_entity("Acme", "company", user_id=owner)
    with pytest.raises(ValueError):
        kg.add_relation("Acme", "company", "Merger", "project", "owns", user_id=owner)
    assert kg.get_stats()["entities"] == 0


def test_same_name_is_a_separate_entity_per_owner_and_updates_stay_local(kg):
    first = kg.add_entity("Acme", "company", {"note": "alice"}, user_id="alice")
    second = kg.add_entity("Acme", "company", {"note": "bob"}, user_id="bob")
    assert first != second
    assert kg.add_entity("Acme", "company", {"note": "alice v2"}, user_id="alice") == first
    assert kg.get_entity("Acme", user_id="alice")["properties"] == {"note": "alice v2"}
    assert kg.get_entity("Acme", user_id="bob")["properties"] == {"note": "bob"}


def test_a_corrupt_cross_owner_link_does_not_surface_the_other_entity(kg):
    _seed(kg)
    db = kg._get_db()
    alice_acme = kg.get_entity("Acme", user_id="alice")["id"]
    bob_secret = kg.get_entity("Layoffs", user_id="bob")["id"]
    db.write(
        "INSERT INTO relations (user_id, source_id, target_id, relation_type, created_at)"
        " VALUES ('alice', ?, ?, 'leaks', '2026-01-01')",
        (alice_acme, bob_secret),
    )
    assert [c["entity"] for c in kg.get_connections("Acme", user_id="alice")] == ["Merger"]


def _legacy_db(path):
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE entities (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, type TEXT NOT NULL,
                properties TEXT DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE relations (id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES entities(id), target_id INTEGER NOT NULL REFERENCES entities(id),
                relation_type TEXT NOT NULL, properties TEXT DEFAULT '{}', created_at TEXT NOT NULL);
            CREATE UNIQUE INDEX idx_entity_unique ON entities(name, type);
            CREATE UNIQUE INDEX idx_relation_unique ON relations(source_id, target_id, relation_type);
            INSERT INTO entities (name, type, created_at, updated_at) VALUES ('Roman', 'person', 't', 't');
            INSERT INTO entities (name, type, created_at, updated_at) VALUES ('KAOS', 'project', 't', 't');
            INSERT INTO relations (source_id, target_id, relation_type, created_at) VALUES (1, 2, 'owns', 't');
            """
        )


def test_legacy_rows_survive_migration_but_belong_to_nobody(kg, tmp_path):
    _legacy_db(tmp_path / "knowledge_graph.db")

    # No requester inherits them — not even one whose id looks like the obvious owner.
    for owner in ("1", "roman", "alice"):
        assert kg.get_graph_context("Roman", user_id=owner) == ""
    assert kg.get_stats()["entities"] == 2 and kg.get_stats()["relations"] == 1
    assert kg.legacy_row_counts() == {"entities": 2, "relations": 1}

    # A new owner can use the same names; the pre-migration unique index would have refused this.
    kg.add_relation("Roman", "person", "KAOS", "project", "admires", user_id="alice")
    assert kg.get_graph_context("Roman", user_id="alice") == "Roman (person): admires→KAOS(project)"
    assert kg.legacy_row_counts() == {"entities": 2, "relations": 1}

    # Re-opening the migrated file is a no-op.
    import kronos.db as db_module

    db_module._instances.clear()
    kg._schema_initialized = False
    assert kg.legacy_row_counts() == {"entities": 2, "relations": 1}


def test_operator_assignment_moves_legacy_rows_without_merging_or_deleting(kg, tmp_path):
    _legacy_db(tmp_path / "knowledge_graph.db")
    kg.add_entity("KAOS", "project", {"mine": True}, user_id="alice")

    with pytest.raises(ValueError):
        kg.assign_legacy_rows("")

    # 'KAOS' collides with Alice's own entity, so it and the relation touching it stay ownerless.
    assert kg.assign_legacy_rows("alice") == {"entities": 1, "relations": 0}
    assert kg.legacy_row_counts() == {"entities": 1, "relations": 1}
    assert kg.get_entity("Roman", user_id="alice") is not None
    assert kg.get_entity("KAOS", user_id="alice")["properties"] == {"mine": True}
    assert kg.get_connections("Roman", user_id="alice") == []
    assert kg.get_stats()["entities"] == 3

    assert kg.assign_legacy_rows("alice") == {"entities": 0, "relations": 0}


def test_operator_assignment_moves_a_whole_legacy_graph(kg, tmp_path):
    _legacy_db(tmp_path / "knowledge_graph.db")
    assert kg.assign_legacy_rows("alice") == {"entities": 2, "relations": 1}
    assert kg.get_graph_context("Roman", user_id="alice") == "Roman (person): owns→KAOS(project)"
    assert kg.get_graph_context("Roman", user_id="bob") == ""


def test_retrieval_injects_only_the_callers_graph(kg, monkeypatch):
    from kronos.memory import nodes

    _seed(kg)
    monkeypatch.setattr(nodes, "search_memories", lambda *args, **kwargs: [])

    def injected(user_id):
        update = nodes.retrieve_memories({"messages": [HumanMessage(content="Acme")], "user_id": user_id})
        return update["messages"][0].content if update else ""

    assert "Merger" in injected("alice") and "Layoffs" not in injected("alice")
    assert "Layoffs" in injected("bob") and "Merger" not in injected("bob")
    assert injected("mallory") == ""
    assert injected("") == ""


async def test_sleep_compute_builds_a_separate_graph_per_user(kg, monkeypatch):
    from kronos.cron import sleep_compute
    from kronos.memory import fts

    fts.index_fact("Alice runs the Merger project", "alice")
    fts.index_fact("Bob plans Layoffs", "bob")
    prompts = []

    class Model:
        async def ainvoke(self, messages):
            prompt = messages[0].content
            prompts.append(prompt)
            name = "Merger" if "Merger" in prompt else "Layoffs"
            body = f'{{"entities": [{{"name": "{name}", "type": "project"}}], "relations": []}}'
            return type("R", (), {"content": body})()

    monkeypatch.setattr(sleep_compute, "get_model", lambda tier: Model())
    facts = sleep_compute._get_recent_facts(days=7)
    assert set(facts) == {"alice", "bob"}
    for user_id, user_facts in facts.items():
        await sleep_compute._extract_and_build_graph(user_facts, user_id)

    # No prompt mixes owners, and each entity lands in its owner's graph only.
    assert not any("Merger" in p and "Layoffs" in p for p in prompts)
    assert kg.get_entity("Merger", user_id="alice") and not kg.get_entity("Merger", user_id="bob")
    assert kg.get_entity("Layoffs", user_id="bob") and not kg.get_entity("Layoffs", user_id="alice")
