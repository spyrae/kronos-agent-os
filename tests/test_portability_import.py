"""Agent bundle import — dedupe, merge modes, dry-run, safety (moat phase 7.3)."""

import json
import sqlite3
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from kronos.config import settings
from kronos.portability import BundleError, import_bundle
from kronos.portability.export import export_bundle
from kronos.workspace import Workspace

FIXED_TIME = "2026-01-01T00:00:00+00:00"


@dataclass
class ImportEnv:
    """A bundle exported from agent 'source', with settings pointing at 'target'."""

    bundle: Path
    space: Workspace
    db_dir: Path
    tmp: Path


def _point_settings_at(monkeypatch, tmp_path, agent: str) -> tuple[Path, Workspace]:
    db_dir = tmp_path / "data" / agent
    db_dir.mkdir(parents=True, exist_ok=True)
    workspace_root = tmp_path / "workspaces" / agent

    monkeypatch.setattr(settings, "agent_name", agent)
    monkeypatch.setattr(settings, "db_dir", str(db_dir))
    monkeypatch.setattr(settings, "db_path", str(db_dir / "session.db"))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "data" / f"swarm-{agent}.db"))
    monkeypatch.setattr(settings, "workspace_path", str(workspace_root))
    monkeypatch.setattr(settings, "shared_workspace_path", "")

    import kronos.db as _db
    import kronos.swarm_store as _swarm
    import kronos.workspace as _ws
    from kronos.memory import fts as _fts
    from kronos.memory import knowledge_graph as _kg

    _db._instances.clear()
    _swarm._singleton = None
    monkeypatch.setattr(_fts, "_schema_initialized", False)
    monkeypatch.setattr(_kg, "_schema_initialized", False)

    space = _ws.Workspace(workspace_root)
    monkeypatch.setattr(_ws, "ws", space)
    space.ensure_dirs()
    return db_dir, space


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Build a bundle from agent 'source', then switch to a blank agent 'target'."""
    _, source_space = _point_settings_at(monkeypatch, tmp_path, "source")

    source_space.identity.write_text("# Кто я\nАгент источника.\n", encoding="utf-8")
    source_space.soul.write_text("Отвечаю коротко.\n", encoding="utf-8")
    (source_space.user_dir / "USER.md").write_text("Роман строит KAOS.\n", encoding="utf-8")

    skill_dir = source_space.skills_dir / "research-brief"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: research-brief\ndescription: Краткий бриф\nstatus: active\n---\n\n# Бриф\nШаги.\n",
        encoding="utf-8",
    )
    (skill_dir / "references" / "SOURCES.md").write_text("- exa\n", encoding="utf-8")

    from kronos import scheduled_tasks
    from kronos.memory import fts, knowledge_graph
    from kronos.swarm_store import get_swarm

    fts.index_fact("Предпочитает краткие ответы", "roman")
    fts.index_fact("Работает из Бали", "roman")
    knowledge_graph.add_relation("Роман", "person", "KAOS", "project", "owns")
    get_swarm().add_shared_fact(user_id="roman", fact="Часовой пояс UTC+8", source_agent="source")
    scheduled_tasks.add_task(
        agent_name="source",
        chat_id=555,
        topic_id=None,
        thread_id="555",
        run_at=1800000000.0,
        message="Проверить релиз",
    )

    conn = sqlite3.connect(settings.db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS sessions (thread_id TEXT PRIMARY KEY, messages TEXT, updated_at TEXT)")
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?)",
        ("555", json.dumps([{"type": "human", "content": "как дела"}]), "2026-01-01 10:00:00"),
    )
    conn.commit()
    conn.close()

    bundle = tmp_path / "source.kaos"
    export_bundle(bundle, include_notes=True, include_sessions=True, created_at=FIXED_TIME)

    db_dir, target_space = _point_settings_at(monkeypatch, tmp_path, "target")
    yield ImportEnv(bundle=bundle, space=target_space, db_dir=db_dir, tmp=tmp_path)

    import kronos.db as _db

    _db._instances.clear()


def _fact_contents() -> list[str]:
    db_path = Path(settings.db_dir) / "memory_fts.db"
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return [row[0] for row in conn.execute("SELECT content FROM memory_facts ORDER BY content")]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def test_import_creates_records(env):
    report = import_bundle(env.bundle)

    assert report.source_agent == "source"
    assert report.created["facts"] == 2
    assert report.created["graph_entities"] == 2
    assert report.created["graph_relations"] == 1
    assert report.created["shared_facts"] == 1
    assert report.created["skills"] == 1

    facts = _fact_contents()
    assert "Работает из Бали" in facts
    assert "Предпочитает краткие ответы" in facts


def test_imported_skills_are_draft_and_marked(env):
    import_bundle(env.bundle)

    from kronos.skills.store import SkillStore

    skill = SkillStore(env.space.root).get("research-brief")
    assert skill is not None
    assert skill.status == "draft"
    assert skill.imported_from == "source"
    assert (env.space.skills_dir / "research-brief" / "references" / "SOURCES.md").exists()


def test_import_is_idempotent(env):
    first = import_bundle(env.bundle)
    second = import_bundle(env.bundle)

    assert first.created["facts"] == 2
    assert second.created.get("facts", 0) == 0
    assert second.skipped["facts"] == 2
    assert second.created.get("skills", 0) == 0
    assert second.skipped["skills"] == 1
    assert len(_fact_contents()) == 2


def test_dedupe_ignores_whitespace_and_case(env):
    from kronos.memory import fts

    fts.index_fact("предпочитает   КРАТКИЕ ответы", "roman")
    report = import_bundle(env.bundle)

    assert report.skipped["facts"] == 1
    assert report.created["facts"] == 1


def test_dry_run_writes_nothing(env):
    report = import_bundle(env.bundle, dry_run=True)

    assert report.dry_run is True
    assert report.created["facts"] == 2
    assert _fact_contents() == []
    assert not (env.space.skills_dir / "research-brief").exists()
    assert not (env.db_dir / "memory_fts.db").exists()


def test_persona_is_kept_local_by_default(env):
    env.space.soul.write_text("Мой собственный голос.\n", encoding="utf-8")

    report = import_bundle(env.bundle)

    assert env.space.soul.read_text(encoding="utf-8") == "Мой собственный голос.\n"
    assert report.skipped["persona_files"] >= 1
    assert any("kept local" in w for w in report.warnings)


def test_persona_append_marks_the_source(env):
    env.space.soul.write_text("Мой собственный голос.\n", encoding="utf-8")

    import_bundle(env.bundle, merge="append")
    content = env.space.soul.read_text(encoding="utf-8")

    assert "Мой собственный голос." in content
    assert "Imported from 'source'" in content
    assert "Отвечаю коротко." in content


def test_persona_overwrite_replaces_file(env):
    env.space.soul.write_text("Мой собственный голос.\n", encoding="utf-8")

    import_bundle(env.bundle, merge="overwrite")

    assert env.space.soul.read_text(encoding="utf-8") == "Отвечаю коротко.\n"


def test_missing_persona_is_created_even_on_skip(env):
    import_bundle(env.bundle)

    assert env.space.soul.exists()
    assert "Отвечаю коротко." in env.space.soul.read_text(encoding="utf-8")


def test_notes_are_imported(env):
    import_bundle(env.bundle)

    assert (env.space.notes_dir / "user" / "USER.md").exists()
    assert "KAOS" in (env.space.notes_dir / "user" / "USER.md").read_text(encoding="utf-8")


def test_sessions_land_in_inbox_not_in_live_history(env):
    report = import_bundle(env.bundle)

    archive = env.space.inbox_dir / "imported-sessions-source.md"
    assert archive.exists()
    assert "thread 555" in archive.read_text(encoding="utf-8")
    assert report.created["session_threads"] == 1
    # The live session store must not be rewritten from a foreign installation.
    assert not (env.db_dir / "session.db").exists()


def test_schedule_needs_rebind_is_skipped_with_reason(env):
    report = import_bundle(env.bundle)

    assert report.skipped["scheduled_tasks"] == 1
    assert any("rebind_chat" in w for w in report.warnings)


def test_schedule_rebinds_to_local_chat(env):
    import_bundle(env.bundle, rebind_chat=999)

    from kronos import scheduled_tasks

    pending = scheduled_tasks.list_pending("target")
    assert len(pending) == 1
    assert pending[0]["chat_id"] == 999
    assert pending[0]["message"] == "Проверить релиз"


def test_unknown_merge_mode_is_rejected(env):
    with pytest.raises(BundleError, match="unknown merge mode"):
        import_bundle(env.bundle, merge="rebase")


def test_missing_bundle_is_rejected(tmp_path):
    with pytest.raises(BundleError, match="not found"):
        import_bundle(tmp_path / "nope.kaos")


def test_tampered_bundle_is_refused_before_writing(env, tmp_path):
    extracted = tmp_path / "unpacked"
    with zipfile.ZipFile(env.bundle) as archive:
        archive.extractall(extracted)
    facts = extracted / "memory" / "facts.jsonl"
    facts.write_text(
        facts.read_text(encoding="utf-8") + '{"content": "внедрённый факт", "user_id": "roman"}\n', encoding="utf-8"
    )

    tampered = tmp_path / "tampered.kaos"
    with zipfile.ZipFile(tampered, "w") as archive:
        for path in sorted(p for p in extracted.rglob("*") if p.is_file()):
            archive.write(path, path.relative_to(extracted).as_posix())

    with pytest.raises(BundleError, match="failed verification"):
        import_bundle(tampered)
    assert _fact_contents() == []


def test_path_traversal_in_bundle_is_refused(tmp_path):
    evil = tmp_path / "evil.kaos"
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr("../escaped.md", "pwned")

    with pytest.raises(BundleError, match="unsafe path"):
        import_bundle(evil)
    assert not (tmp_path.parent / "escaped.md").exists()
