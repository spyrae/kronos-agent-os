"""Agent bundle export — content, determinism, secret containment (moat phase 7.2)."""

import json
import sqlite3
import zipfile

import pytest

from kronos.config import settings

FIXED_TIME = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def agent_env(tmp_path, monkeypatch):
    """A populated agent: workspace files, facts, graph, shared facts, schedule, sessions."""
    db_dir = tmp_path / "data" / "kronos"
    db_dir.mkdir(parents=True)
    workspace_root = tmp_path / "workspaces" / "kronos"

    monkeypatch.setattr(settings, "agent_name", "kronos")
    monkeypatch.setattr(settings, "db_dir", str(db_dir))
    monkeypatch.setattr(settings, "db_path", str(db_dir / "session.db"))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "data" / "swarm.db"))
    monkeypatch.setattr(settings, "workspace_path", str(workspace_root))
    monkeypatch.setattr(settings, "shared_workspace_path", "")

    import kronos.db as _db
    import kronos.swarm_store as _swarm
    import kronos.workspace as _ws
    from kronos.memory import fts as _fts
    from kronos.memory import knowledge_graph as _kg

    _db._instances.clear()
    _swarm._singleton = None
    # fts/knowledge_graph cache "schema is ready" in a module flag, so a fresh
    # temp database needs the flag cleared or writes hit a schemaless file.
    monkeypatch.setattr(_fts, "_schema_initialized", False)
    monkeypatch.setattr(_kg, "_schema_initialized", False)
    space = _ws.Workspace(workspace_root)
    monkeypatch.setattr(_ws, "ws", space)
    space.ensure_dirs()

    # Persona
    space.identity.write_text("# Кто я\nОператор Романа.\n", encoding="utf-8")
    space.soul.write_text("Говорю прямо. Токен: sk-secretsecretsecret1234\n", encoding="utf-8")

    # A skill with a reference
    skill_dir = space.skills_dir / "research-brief"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: research-brief\ndescription: Краткий бриф\n---\n\n# Бриф\n", encoding="utf-8"
    )
    (skill_dir / "references" / "SOURCES.md").write_text("- exa\n- brave\n", encoding="utf-8")

    # Notes
    (space.user_dir / "USER.md").write_text("Роман. Работает над KAOS.\n", encoding="utf-8")

    # Things that must never be exported
    (workspace_root / ".env").write_text("DEEPSEEK_API_KEY=sk-live-key\n", encoding="utf-8")
    (workspace_root / "kronos.session").write_text("telethon-session-blob", encoding="utf-8")
    (space.notes_dir / "secrets.session").write_text("blob", encoding="utf-8")

    # Facts + graph via the real APIs so schemas cannot drift from production
    from kronos.memory import fts, knowledge_graph

    fts.index_fact("Предпочитает краткие технические ответы", "roman")
    fts.index_fact("Ключ доступа Bearer abcdefghijklmnop12345", "roman")
    knowledge_graph.add_entity("KAOS", "project", {"stack": "python"})
    knowledge_graph.add_relation("Роман", "person", "KAOS", "project", "owns")

    # Shared facts: one from this agent, one from a peer (must not be exported)
    from kronos.swarm_store import get_swarm

    store = get_swarm()
    store.add_shared_fact(user_id="roman", fact="Живёт в Бали", source_agent="kronos")
    store.add_shared_fact(user_id="roman", fact="Факт от соседа", source_agent="nexus")

    # Schedule
    from kronos import scheduled_tasks

    scheduled_tasks.add_task(
        agent_name="kronos",
        chat_id=123456789,
        topic_id=42,
        thread_id="123456789:42",
        run_at=1800000000.0,
        message="Напомнить про релиз",
    )

    # Sessions (SessionStore is async; the schema is stable enough to seed directly)
    conn = sqlite3.connect(db_dir / "session.db")
    conn.execute("CREATE TABLE IF NOT EXISTS sessions (thread_id TEXT PRIMARY KEY, messages TEXT, updated_at TEXT)")
    conn.execute(
        "INSERT INTO sessions (thread_id, messages, updated_at) VALUES (?, ?, ?)",
        (
            "123456789",
            json.dumps([{"type": "human", "content": "напиши на roman@example.com"}]),
            "2026-01-01 10:00:00",
        ),
    )
    conn.commit()
    conn.close()

    yield tmp_path
    _db._instances.clear()
    _swarm._singleton = None


def _export(tmp_path, **kwargs):
    from kronos.portability.export import export_bundle

    return export_bundle(tmp_path / "out" / "agent.kaos", created_at=FIXED_TIME, **kwargs)


def _bundle_files(path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name).decode("utf-8") for name in archive.namelist()}


def test_export_writes_all_default_sections(agent_env):
    report = _export(agent_env)
    files = _bundle_files(report.path)

    assert "manifest.json" in files
    assert "persona/IDENTITY.md" in files
    assert "skills/research-brief/SKILL.md" in files
    assert "skills/research-brief/references/SOURCES.md" in files
    assert "memory/facts.jsonl" in files
    assert "memory/graph.entities.jsonl" in files
    assert "memory/graph.relations.jsonl" in files
    assert "memory/shared_facts.jsonl" in files
    assert "schedule/tasks.jsonl" in files
    # Opt-in sections stay out unless asked for.
    assert not any(name.startswith("notes/") for name in files)
    assert not any(name.startswith("sessions/") for name in files)


def test_manifest_counts_match_payload(agent_env):
    report = _export(agent_env)
    files = _bundle_files(report.path)

    assert report.counts["persona_files"] == 2
    assert report.counts["skills"] == 1
    assert report.counts["facts"] == len(files["memory/facts.jsonl"].strip().splitlines())
    assert report.counts["graph_entities"] == 2  # KAOS(project) + Роман(person); KAOS is reused by the relation
    assert report.counts["graph_relations"] == 1
    assert report.counts["shared_facts"] == 1
    assert report.counts["scheduled_tasks"] == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"include_notes": True},
        {"include_sessions": True},
        {"include_notes": True, "include_sessions": True, "include_transport_ids": True},
    ],
)
def test_export_never_contains_secrets(agent_env, kwargs):
    report = _export(agent_env, **kwargs)
    files = _bundle_files(report.path)

    assert not any(name.endswith((".env", ".session")) for name in files)
    assert not any(name.endswith((".db", ".db-wal", ".sqlite")) for name in files)
    blob = "\n".join(files.values())
    assert "sk-secretsecretsecret1234" not in blob
    assert "sk-live-key" not in blob
    assert "telethon-session-blob" not in blob
    assert "abcdefghijklmnop12345" not in blob


def test_export_redacts_secrets_but_keeps_owner_content(agent_env):
    files = _bundle_files(_export(agent_env).path)

    assert "Говорю прямо." in files["persona/SOUL.md"]
    assert "sk-***REDACTED***" in files["persona/SOUL.md"]
    facts = files["memory/facts.jsonl"]
    assert "Предпочитает краткие технические ответы" in facts
    assert "Bearer ***REDACTED***" in facts


def test_export_is_byte_identical_for_unchanged_state(agent_env):
    first = _export(agent_env)
    first_bytes = first.path.read_bytes()
    first.path.unlink()
    second = _export(agent_env)

    assert second.path.read_bytes() == first_bytes
    assert second.manifest.artifacts == first.manifest.artifacts


def test_export_bundle_verifies_against_its_manifest(agent_env, tmp_path):
    from kronos.portability.manifest import verify_manifest

    report = _export(agent_env)
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(report.path) as archive:
        archive.extractall(extracted)

    assert verify_manifest(extracted) == []


def test_shared_facts_exclude_other_agents(agent_env):
    files = _bundle_files(_export(agent_env).path)
    shared = files["memory/shared_facts.jsonl"]

    assert "Живёт в Бали" in shared
    assert "Факт от соседа" not in shared


def test_schedule_drops_transport_ids_by_default(agent_env):
    files = _bundle_files(_export(agent_env).path)
    task = json.loads(files["schedule/tasks.jsonl"].strip())

    assert task["needs_rebind"] is True
    assert task["chat_id"] is None and task["thread_id"] is None
    assert task["message"] == "Напомнить про релиз"


def test_schedule_keeps_transport_ids_when_requested(agent_env):
    files = _bundle_files(_export(agent_env, include_transport_ids=True).path)
    task = json.loads(files["schedule/tasks.jsonl"].strip())

    assert task["needs_rebind"] is False
    assert task["chat_id"] == 123456789
    assert task["topic_id"] == 42


def test_notes_are_included_on_demand(agent_env):
    report = _export(agent_env, include_notes=True)
    files = _bundle_files(report.path)

    assert "notes/user/USER.md" in files
    assert "Работает над KAOS" in files["notes/user/USER.md"]
    assert "notes" in report.manifest.includes


def test_sessions_are_included_on_demand_and_pii_masked(agent_env):
    report = _export(agent_env, include_sessions=True)
    files = _bundle_files(report.path)
    row = json.loads(files["sessions/sessions.jsonl"].strip())

    assert row["thread_id"] == "123456789"
    assert "roman@example.com" not in json.dumps(row, ensure_ascii=False)
    assert "sessions" in report.manifest.includes


def test_export_on_empty_agent_reports_warning(tmp_path, monkeypatch):
    """A fresh agent with no memory exports a valid, mostly-empty bundle."""
    db_dir = tmp_path / "data" / "empty"
    db_dir.mkdir(parents=True)
    monkeypatch.setattr(settings, "agent_name", "empty")
    monkeypatch.setattr(settings, "db_dir", str(db_dir))
    monkeypatch.setattr(settings, "db_path", str(db_dir / "session.db"))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))

    import kronos.db as _db
    import kronos.workspace as _ws

    _db._instances.clear()
    space = _ws.Workspace(tmp_path / "workspaces" / "empty")
    monkeypatch.setattr(_ws, "ws", space)

    from kronos.portability.export import export_bundle

    report = export_bundle(tmp_path / "empty.kaos", created_at=FIXED_TIME)

    assert report.path.exists()
    assert report.counts["facts"] == 0
    assert "no extracted facts found" in report.warnings
    # Reading a non-existent database must not create one.
    assert not (db_dir / "memory_fts.db").exists()
    _db._instances.clear()
