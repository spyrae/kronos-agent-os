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
    payload = (
        conversations
        if conversations is not None
        else [
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
    )
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


def _claude_project(tmp_path):
    project = tmp_path / "claude-project"
    (project / "skills" / "brief" / "references").mkdir(parents=True)
    (project / "docs").mkdir()
    (project / "CLAUDE.md").write_text("Ты аналитик. Отвечай по-русски.\n", encoding="utf-8")
    (project / "skills" / "brief" / "SKILL.md").write_text(
        "---\nname: brief\ndescription: Бриф\n---\n\n# Бриф\n", encoding="utf-8"
    )
    (project / "skills" / "brief" / "references" / "SOURCES.md").write_text("- exa\n", encoding="utf-8")
    (project / "docs" / "spec.md").write_text("# Спека\nДетали.\n", encoding="utf-8")
    return project


def _telegram_export(tmp_path):
    export = tmp_path / "tg"
    export.mkdir()
    payload = {
        "personal_information": {
            "first_name": "Роман",
            "last_name": "Белов",
            "username": "spyrae",
            "bio": "строю агентов",
        },
        "chats": {
            "list": [
                {
                    "name": "Рабочий чат",
                    "id": 555,
                    "type": "personal_chat",
                    "messages": [
                        {"id": 1, "type": "message", "from": "Роман", "text": "готов релиз?"},
                        {
                            "id": 2,
                            "type": "message",
                            "from": "Коллега",
                            "text": [{"type": "plain", "text": "почти, "}, {"type": "link", "text": "смотри тут"}],
                        },
                        {"id": 3, "type": "service", "action": "pin_message"},
                    ],
                },
                {"name": "Личное", "id": 777, "type": "personal_chat", "messages": [{"type": "message", "text": "х"}]},
            ]
        },
    }
    (export / "result.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return export


def _letta_agent(tmp_path):
    agent = tmp_path / "agent.af"
    agent.write_text(
        json.dumps(
            {
                "agent_type": "memgpt_agent",
                "name": "Sam",
                "system": "Ты помогаешь с исследованиями.",
                "memory_blocks": [
                    {"label": "persona", "value": "Я вдумчивый ассистент."},
                    {"label": "human", "value": "- Роман\n- Работает над KAOS\n"},
                    {"label": "project_notes", "value": "Спринт до пятницы."},
                ],
                "messages": [
                    {"role": "user", "text": "привет"},
                    {"role": "assistant", "content": "здравствуй"},
                    {"role": "tool", "text": "{}"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return agent


def test_registry_lists_importers():
    assert set(available()) == {"chatgpt", "claude-projects", "letta", "telegram", "obsidian"}


def test_strict_importers_are_probed_before_obsidian(tmp_path):
    """A markdown folder with CLAUDE.md is a Claude project, not a vault."""
    assert detect_importer(_claude_project(tmp_path)) == "claude-projects"
    assert available().index("claude-projects") < available().index("obsidian")


def test_claude_project_maps_instructions_skills_and_docs(tmp_path):
    result = get_importer("claude-projects").to_bundle(
        _claude_project(tmp_path), tmp_path / "cp.kaos", created_at=FIXED_TIME
    )
    files = _bundle_files(result.bundle)

    assert "Ты аналитик" in files["persona/IDENTITY.md"]
    assert "skills/brief/SKILL.md" in files
    assert "skills/brief/references/SOURCES.md" in files
    assert "notes/world/claude-project/docs/spec.md" in files
    # The instructions file becomes persona, not a duplicate note.
    assert not any(name.endswith("claude-project/CLAUDE.md") for name in files)


def test_claude_projects_json_variant(tmp_path):
    payload = [
        {
            "uuid": "abc",
            "name": "KAOS",
            "description": "Агентная ОС",
            "prompt_template": "Будь краток.",
            "docs": [{"filename": "notes.md", "content": "# Заметки\n"}],
        }
    ]
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert detect_importer(path) == "claude-projects"
    result = get_importer("claude-projects").to_bundle(path, tmp_path / "cp.kaos", created_at=FIXED_TIME)
    files = _bundle_files(result.bundle)

    assert "Будь краток." in files["persona/IDENTITY.md"]
    assert "notes/world/claude-project/KAOS/notes.md" in files
    assert "notes/world/claude-project/KAOS/README.md" in files


def test_telegram_requires_explicit_chat_selection(tmp_path):
    export = _telegram_export(tmp_path)

    assert detect_importer(export) == "telegram"
    result = get_importer("telegram").to_bundle(export, tmp_path / "tg.kaos", user_id="roman", created_at=FIXED_TIME)

    assert result.counts["sessions"] == 0
    assert any("no chats selected" in w for w in result.warnings)
    # Owner identity still comes through — that is why the bundle is not empty.
    assert result.counts["facts"] == 3


def test_telegram_imports_only_selected_chats(tmp_path):
    result = get_importer("telegram").to_bundle(
        _telegram_export(tmp_path),
        tmp_path / "tg.kaos",
        user_id="roman",
        created_at=FIXED_TIME,
        chats=["Рабочий чат"],
    )
    files = _bundle_files(result.bundle)
    session = json.loads(files["sessions/sessions.jsonl"].strip())

    assert session["thread_id"] == "telegram:555"
    contents = [message["content"] for message in session["messages"]]
    assert "Роман: готов релиз?" in contents
    assert "Коллега: почти, смотри тут" in contents  # entity list flattened
    assert len(contents) == 2  # the service message is dropped
    assert not any('"777"' in name for name in files)


def test_telegram_message_cap_is_reported(tmp_path):
    result = get_importer("telegram").to_bundle(
        _telegram_export(tmp_path),
        tmp_path / "tg.kaos",
        created_at=FIXED_TIME,
        chats=["555"],
        limit=1,
    )

    assert any("kept the last 1 messages" in w for w in result.warnings)


def test_letta_maps_blocks_to_persona_facts_and_notes(tmp_path):
    agent = _letta_agent(tmp_path)

    assert detect_importer(agent) == "letta"
    result = get_importer("letta").to_bundle(agent, tmp_path / "letta.kaos", user_id="roman", created_at=FIXED_TIME)
    files = _bundle_files(result.bundle)

    assert "Я вдумчивый ассистент." in files["persona/IDENTITY.md"]
    assert "Ты помогаешь с исследованиями." in files["persona/methodology.md"]
    facts = files["memory/facts.jsonl"]
    assert "Работает над KAOS" in facts
    assert "- Роман" not in facts  # list bullets stripped
    # An unknown block is kept as a note instead of being dropped.
    assert "notes/world/letta/project_notes.md" in files
    session = json.loads(files["sessions/sessions.jsonl"].strip())
    assert [m["type"] for m in session["messages"]] == ["human", "ai", "tool"]


def test_letta_rejects_unrelated_json(tmp_path):
    path = tmp_path / "random.json"
    path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")

    assert get_importer("letta").detect(path) is False
    with pytest.raises(BundleError, match="does not look like a Letta agent file"):
        get_importer("letta").to_bundle(path, tmp_path / "out.kaos")


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
