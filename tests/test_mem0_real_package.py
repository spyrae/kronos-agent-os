"""F12/F13 acceptance against the real mem0ai package, with no network.

Skipped where mem0ai is not installed. The Memory object, its Qdrant store, its
history database and its parsing are real; only the two things that would leave
the machine are replaced: the embedder and the chat-completions call.
"""

import hashlib
import json
import os
import tempfile
from types import SimpleNamespace

import pytest

# Before mem0 is imported, because it reads both at import: no usage telemetry,
# and its config directory somewhere other than the developer's home.
os.environ.setdefault("MEM0_TELEMETRY", "False")
os.environ.setdefault("MEM0_DIR", tempfile.mkdtemp(prefix="kaos-mem0-test-"))

mem0 = pytest.importorskip("mem0")

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from kronos.audit import reset_tool_audit_context, set_tool_audit_context  # noqa: E402
from kronos.config import settings  # noqa: E402
from kronos.memory import reset as reset_module  # noqa: E402
from kronos.memory.model_boundary import BudgetedMemory  # noqa: E402
from kronos.security import cost_guardian  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore")
DIMS = 64


class Embedder:
    """Deterministic local vectors: the same text always lands on the same point."""

    def embed(self, text, memory_action=None):
        digest = hashlib.sha256(text.encode()).digest()
        return [(byte / 255.0) - 0.5 for byte in (digest * (DIMS // len(digest) + 1))[:DIMS]]


class Provider:
    """Stands in for the DeepSeek HTTP endpoint and counts what reached it."""

    def __init__(self):
        self.requests: list[dict] = []
        self.pending_facts: list[str] = []

    def create(self, **request):
        self.requests.append(request)
        system = request["messages"][0]["content"]
        user = request["messages"][-1]["content"]
        if "memory" in system.lower() and "event" in (system + user).lower() and self.pending_facts:
            body = {"memory": [{"id": str(i), "text": f, "event": "ADD"} for i, f in enumerate(self.pending_facts)]}
            self.pending_facts = []
        else:
            self.pending_facts = [
                f"{line.split(':', 1)[1].strip()}" for line in user.splitlines() if line.lower().startswith("user:")
            ]
            body = {"facts": list(self.pending_facts)}
        message = SimpleNamespace(content=json.dumps(body), tool_calls=None)
        usage = SimpleNamespace(prompt_tokens=120, completion_tokens=30)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


@pytest.fixture
def real(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "mem0_qdrant_path", str(tmp_path / "qdrant"))
    monkeypatch.setattr(settings, "deepseek_api_key", "not-a-real-key")
    monkeypatch.setattr(settings, "agent_name", "kronos")
    import kronos.db as db_module
    import kronos.swarm_store as swarm_module
    from kronos.memory import fts, knowledge_graph, store

    db_module._instances.clear()
    swarm_module._singleton = None
    monkeypatch.setattr(knowledge_graph, "_schema_initialized", False)
    monkeypatch.setattr(fts, "_schema_initialized", False)
    monkeypatch.setattr(reset_module, "_gate", reset_module._WriterGate())
    guardian = cost_guardian.CostGuardian(daily_limit=5.0, session_limit=1.0)
    monkeypatch.setattr(cost_guardian, "_guardian", guardian)
    monkeypatch.setattr(guardian, "should_degrade", lambda: False)

    memory = mem0.Memory.from_config(
        {
            "version": "v1.1",
            "llm": {"provider": "deepseek", "config": {"model": "deepseek-chat", "api_key": "not-a-real-key"}},
            "embedder": {"provider": "openai", "config": {"api_key": "not-a-real-key", "embedding_dims": DIMS}},
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": "kronos_memories",
                    "path": str(tmp_path / "qdrant"),
                    "embedding_model_dims": DIMS,
                },
            },
            "history_db_path": str(tmp_path / "history.db"),
        }
    )
    memory.embedding_model = Embedder()
    provider = Provider()
    memory.llm.client = SimpleNamespace(chat=SimpleNamespace(completions=provider))
    wrapped = BudgetedMemory(memory)
    monkeypatch.setattr(store, "get_memory", lambda: wrapped)
    token = set_tool_audit_context(agent="kronos", session_id="chat", thread_id="chat")
    yield SimpleNamespace(memory=memory, wrapped=wrapped, provider=provider)
    reset_tool_audit_context(token)
    db_module._instances.clear()
    swarm_module._singleton = None


def _remember(user_id, text):
    from kronos.memory import nodes

    nodes.store_memories_background(
        {"messages": [HumanMessage(content=text), AIMessage(content="noted")], "user_id": user_id, "session_id": "s"}
    )


def _stored(real, user_id):
    return sorted(item["memory"] for item in real.memory.get_all(user_id=user_id, limit=1000)["results"])


def test_the_installed_package_is_the_version_production_runs():
    from importlib.metadata import version

    assert version("mem0ai") == "1.0.7"


def test_extraction_goes_through_the_budget_boundary_and_settles_its_holds(real):
    from kronos.swarm_store import get_swarm

    _remember("alice", "my passport expires in March")

    assert _stored(real, "alice") == ["my passport expires in March"]
    assert len(real.provider.requests) == 2, "extraction and the update pass both reached the provider"
    assert get_swarm().budget_holds() == {"held": 0.0, "unknown": 0.0}
    assert get_swarm().session_cost("chat") > 0
    assert get_swarm().daily_cost()["requests"] == 2


def test_an_exhausted_budget_stops_extraction_before_the_provider(real, monkeypatch):
    monkeypatch.setattr(cost_guardian, "_guardian", cost_guardian.CostGuardian(daily_limit=0.0, session_limit=1.0))

    _remember("alice", "my passport expires in March")

    assert real.provider.requests == []
    assert _stored(real, "alice") == []


def test_memories_are_stored_and_searched_per_user(real):
    from kronos.memory.store import search_memories

    _remember("alice", "my passport expires in March")
    _remember("bob", "my passport is at the embassy")

    assert _stored(real, "alice") == ["my passport expires in March"]
    assert _stored(real, "bob") == ["my passport is at the embassy"]
    # Hybrid recall: the extracted fact from Mem0 plus the raw turn from the keyword index.
    recalled = search_memories("my passport expires in March", user_id="alice")
    assert "my passport expires in March" in recalled
    assert "embassy" not in " ".join(recalled + search_memories("passport", user_id="alice"))
    assert search_memories("passport", user_id="mallory") == []


async def test_user_reset_deletes_that_users_vectors_and_nobody_elses(real):
    _remember("alice", "my passport expires in March")
    _remember("bob", "my passport is at the embassy")

    report = await reset_module.reset_memory({"facts", "shared"}, user_id="alice")

    assert report["ok"] and report["layers"]["facts"]["mem0"] == "deleted"
    assert _stored(real, "alice") == []
    assert _stored(real, "bob") == ["my passport is at the embassy"]


async def test_global_reset_recreates_a_usable_store(real):
    """Mem0 swaps its vector-store handle during reset; the cached object must follow it."""
    _remember("alice", "my passport expires in March")

    report = await reset_module.reset_memory({"facts"})

    assert report["ok"]
    assert _stored(real, "alice") == []
    _remember("alice", "I moved to Lisbon")
    assert _stored(real, "alice") == ["I moved to Lisbon"]


async def test_a_turn_older_than_the_reset_cannot_write_its_facts_back(real):
    from kronos.memory import nodes

    captured = reset_module.memory_generation("alice")
    await reset_module.reset_memory(set(reset_module.SCOPES), user_id="alice")

    nodes.store_memories_background(
        {
            "messages": [HumanMessage(content="my passport expires in March"), AIMessage(content="noted")],
            "user_id": "alice",
            "session_id": "s",
            "memory_generation": captured,
        }
    )

    assert _stored(real, "alice") == [] and real.provider.requests == []


def test_deleting_one_fact_finds_it_past_the_default_page_of_a_hundred(real):
    from kronos.memory import fts

    for index in range(105):
        real.memory.add(f"filler fact number {index}", user_id="alice", infer=False)
    _remember("alice", "my passport expires in March")
    assert len(real.memory.get_all(user_id="alice")["results"]) == 100, "the package pages at 100 by default"

    row = fts._get_db().read_one(
        "SELECT id FROM memory_facts WHERE user_id = 'alice' AND content = 'my passport expires in March'"
    )
    deleted = reset_module.forget_fact(row["id"])

    assert deleted["mem0"] == 1 and deleted["fts"] == 1
    assert "my passport expires in March" not in _stored(real, "alice")
    assert len(_stored(real, "alice")) == 105


def test_only_reviewed_operations_are_reachable_on_the_wrapper(real):
    with pytest.raises(AttributeError):
        real.wrapped.update("id", "text")
    with pytest.raises(ValueError):
        real.wrapped.delete_all(user_id="")
