"""Foreign-export importers → .kaos bundle (moat phase 7.4)."""

import json
import zipfile

import pytest

from kronos.portability import BundleError
from kronos.portability.importers import available, detect_importer, get_importer

FIXED_TIME = "2026-01-01T00:00:00+00:00"


def _bundle_files(path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name).decode("utf-8") for name in archive.namelist()}


def _chatgpt_export(tmp_path, conversations=None):
    """A minimal ChatGPT export: a message tree with one regenerated branch."""
    # `is None` rather than falsy: an explicitly empty export is a valid case.
    payload = conversations if conversations is not None else [
        {
            "title": "Планы на KAOS",
            "create_time": 1700000000.0,
            "update_time": 1700000500.0,
            "mapping": {
                "root": {"id": "root", "message": None, "parent": None, "children": ["m1"]},
                "m1": {
                    "id": "m1",
                    "parent": "root",
                    "children": ["m2", "m2-alt"],
                    "message": {
                        "author": {"role": "user"},
                        "create_time": 1700000100.0,
                        "content": {"parts": ["Запомни: я предпочитаю краткие ответы. Ещё поговорим о KAOS."]},
                    },
                },
                "m2-alt": {
                    "id": "m2-alt",
                    "parent": "m1",
                    "children": [],
                    "message": {
                        "author": {"role": "assistant"},
                        "create_time": 1700000200.0,
                        "content": {"parts": ["Отброшенная ветка."]},
                    },
                },
                "m2": {
                    "id": "m2",
                    "parent": "m1",
                    "children": [],
                    "message": {
                        "author": {"role": "assistant"},
                        "create_time": 1700000400.0,
                        "content": {"parts": ["Понял, буду краток."]},
                    },
                },
            },
        }
    ]
    export_dir = tmp_path / "chatgpt-export"
    export_dir.mkdir()
    (export_dir / "conversations.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return export_dir


def _vault(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "projects").mkdir()
    (vault / "projects" / "KAOS.md").write_text(
        "---\ntype: note\n---\n\nСвязан с [[Роман]] и [[Bali]].\n", encoding="utf-8"
    )
    (vault / "Роман.md").write_text(
        "---\ntype: fact\ndate: 2026-01-01\n---\n\nРоман работает из Бали.\n", encoding="utf-8"
    )
    (vault / "prefs.md").write_text(
        "---\nfacts: [Любит краткость, Пишет по-русски]\n---\n\nНастройки.\n", encoding="utf-8"
    )
    (vault / ".obsidian" / "workspace.json").write_text("{}", encoding="utf-8")
    (vault / ".trash").mkdir()
    (vault / ".trash" / "deleted.md").write_text("удалённая заметка\n", encoding="utf-8")
    return vault


def test_registry_lists_importers():
    assert "chatgpt" in available()
    assert "obsidian" in available()


def test_unknown_importer_is_rejected():
    with pytest.raises(BundleError, match="unknown importer"):
        get_importer("myspace")


def test_detect_picks_the_right_importer(tmp_path):
    assert detect_importer(_chatgpt_export(tmp_path)) == "chatgpt"
    assert detect_importer(_vault(tmp_path)) == "obsidian"
    assert detect_importer(tmp_path / "nothing-here") is None


def test_chatgpt_keeps_only_the_retained_branch(tmp_path):
    result = get_importer("chatgpt").to_bundle(
        _chatgpt_export(tmp_path), tmp_path / "out.kaos", user_id="roman", created_at=FIXED_TIME
    )
    files = _bundle_files(result.bundle)
    session = json.loads(files["sessions/sessions.jsonl"].strip())

    contents = [message["content"] for message in session["messages"]]
    assert "Понял, буду краток." in contents
    assert "Отброшенная ветка." not in contents
    assert [message["type"] for message in session["messages"]] == ["human", "ai"]


def test_chatgpt_harvests_explicit_memory_statements(tmp_path):
    result = get_importer("chatgpt").to_bundle(
        _chatgpt_export(tmp_path), tmp_path / "out.kaos", user_id="roman", created_at=FIXED_TIME
    )
    facts = _bundle_files(result.bundle)["memory/facts.jsonl"]

    assert "я предпочитаю краткие ответы" in facts.lower()
    # A sentence without a memory marker is not a fact.
    assert "поговорим о KAOS" not in facts


def test_chatgpt_reads_an_export_zip(tmp_path):
    export_dir = _chatgpt_export(tmp_path)
    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(export_dir / "conversations.json", "chatgpt-export/conversations.json")

    assert get_importer("chatgpt").detect(archive) is True
    result = get_importer("chatgpt").to_bundle(archive, tmp_path / "out.kaos", created_at=FIXED_TIME)
    assert result.counts["sessions"] == 1


def test_chatgpt_limit_reports_what_was_dropped(tmp_path):
    conversations = []
    for index in range(3):
        conversations.append(
            {
                "title": f"Разговор {index}",
                "update_time": 1700000000.0 + index,
                "mapping": {
                    f"m{index}": {
                        "id": f"m{index}",
                        "parent": None,
                        "message": {
                            "author": {"role": "user"},
                            "create_time": 1700000000.0 + index,
                            "content": {"parts": [f"сообщение {index}"]},
                        },
                    }
                },
            }
        )

    result = get_importer("chatgpt").to_bundle(
        _chatgpt_export(tmp_path, conversations), tmp_path / "out.kaos", limit=2, created_at=FIXED_TIME
    )

    assert result.counts["sessions"] == 2
    assert any("1 older ones skipped" in w for w in result.warnings)


def test_chatgpt_empty_export_is_an_explicit_error(tmp_path):
    result_dir = _chatgpt_export(tmp_path, [])
    with pytest.raises(BundleError, match="nothing importable"):
        get_importer("chatgpt").to_bundle(result_dir, tmp_path / "out.kaos")


def test_obsidian_maps_notes_links_and_facts(tmp_path):
    result = get_importer("obsidian").to_bundle(
        _vault(tmp_path), tmp_path / "vault.kaos", user_id="roman", created_at=FIXED_TIME
    )
    files = _bundle_files(result.bundle)

    assert "notes/world/vault/projects/KAOS.md" in files
    assert "notes/world/vault/Роман.md" in files
    # Vault plumbing and trash stay out.
    assert not any("workspace.json" in name for name in files)
    assert not any("deleted" in name for name in files)

    relations = files["memory/graph.relations.jsonl"]
    assert '"name": "Роман"' in relations and "links_to" in relations
    assert '"name": "Bali"' in relations

    facts = files["memory/facts.jsonl"]
    assert "Роман работает из Бали." in facts
    assert "Любит краткость" in facts
    assert "Пишет по-русски" in facts


def test_obsidian_requires_a_directory(tmp_path):
    lonely = tmp_path / "note.md"
    lonely.write_text("текст\n", encoding="utf-8")

    assert get_importer("obsidian").detect(lonely) is False
    with pytest.raises(BundleError, match="not a vault directory"):
        get_importer("obsidian").to_bundle(lonely, tmp_path / "out.kaos")


def test_importer_bundles_verify_and_import(tmp_path, monkeypatch):
    """The whole point: a foreign export becomes a bundle the normal path accepts."""
    from kronos.config import settings
    from kronos.portability import import_bundle
    from kronos.portability.manifest import verify_manifest

    result = get_importer("obsidian").to_bundle(_vault(tmp_path), tmp_path / "vault.kaos", created_at=FIXED_TIME)

    extracted = tmp_path / "unpacked"
    with zipfile.ZipFile(result.bundle) as archive:
        archive.extractall(extracted)
    assert verify_manifest(extracted) == []

    db_dir = tmp_path / "data" / "target"
    db_dir.mkdir(parents=True)
    monkeypatch.setattr(settings, "agent_name", "target")
    monkeypatch.setattr(settings, "db_dir", str(db_dir))
    monkeypatch.setattr(settings, "db_path", str(db_dir / "session.db"))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "shared_workspace_path", "")

    import kronos.db as _db
    import kronos.workspace as _ws
    from kronos.memory import fts as _fts
    from kronos.memory import knowledge_graph as _kg

    _db._instances.clear()
    monkeypatch.setattr(_fts, "_schema_initialized", False)
    monkeypatch.setattr(_kg, "_schema_initialized", False)
    space = _ws.Workspace(tmp_path / "workspaces" / "target")
    monkeypatch.setattr(_ws, "ws", space)
    space.ensure_dirs()

    report = import_bundle(result.bundle)

    assert report.source_agent == "obsidian"
    assert report.created["facts"] == 3
    assert report.created["notes"] == 3
    assert (space.notes_dir / "world" / "vault" / "Роман.md").exists()
    _db._instances.clear()
