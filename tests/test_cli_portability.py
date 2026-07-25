"""CLI surface for export/import/import-from (moat phase 7.5)."""

import json
import zipfile

import pytest

from kronos.cli import build_parser, main
from kronos.config import settings


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """An agent with one fact and one persona file, isolated in tmp_path."""
    db_dir = tmp_path / "data" / "cli"
    db_dir.mkdir(parents=True)

    monkeypatch.setattr(settings, "agent_name", "cli")
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
    space = _ws.Workspace(tmp_path / "workspaces" / "cli")
    monkeypatch.setattr(_ws, "ws", space)
    space.ensure_dirs()
    space.soul.write_text("Отвечаю по делу.\n", encoding="utf-8")

    from kronos.memory import fts

    fts.index_fact("Работает над KAOS", "roman")

    yield tmp_path
    _db._instances.clear()


def test_parser_exposes_portability_commands():
    parser = build_parser()

    args = parser.parse_args(["export", "--out", "a.kaos", "--include-notes"])
    assert args.command == "export" and args.include_notes is True

    args = parser.parse_args(["import", "a.kaos", "--merge", "append", "--dry-run", "--rebind-chat", "42"])
    assert args.merge == "append" and args.dry_run is True and args.rebind_chat == 42

    args = parser.parse_args(["import-from", "auto", "./export", "--limit", "5", "--chat", "Work", "--chat", "123"])
    assert args.tool == "auto" and args.limit == 5 and args.chat == ["Work", "123"]


def test_import_from_rejects_unknown_tool():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["import-from", "myspace", "./export"])


def test_export_then_import_round_trip(agent, capsys):
    bundle = agent / "cli.kaos"

    assert main(["export", "--out", str(bundle)]) == 0
    out = capsys.readouterr().out
    assert "Exported agent 'cli'" in out
    assert bundle.exists()

    assert main(["import", str(bundle), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Would import bundle from agent 'cli'" in out
    assert "Nothing was written" in out


def test_export_warns_when_transport_ids_are_kept(agent, capsys):
    assert main(["export", "--out", str(agent / "with-ids.kaos"), "--include-transport-ids"]) == 0

    assert "treat it as private" in capsys.readouterr().out


def test_import_of_missing_bundle_fails_cleanly(agent, capsys):
    assert main(["import", str(agent / "absent.kaos")]) == 1

    assert "Import failed:" in capsys.readouterr().out


def test_import_from_auto_detects_and_imports(agent, capsys):
    vault = agent / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "note.md").write_text("---\ntype: fact\n---\n\nЛюбит краткость.\n", encoding="utf-8")

    assert main(["import-from", "auto", str(vault)]) == 0
    out = capsys.readouterr().out

    assert "Detected importer: obsidian" in out
    assert "Converted obsidian export" in out
    assert "Imported bundle from agent 'obsidian'" in out


def test_import_from_convert_only_keeps_the_bundle(agent, capsys):
    vault = agent / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("Заметка.\n", encoding="utf-8")
    out_bundle = agent / "vault.kaos"

    assert main(["import-from", "obsidian", str(vault), "--convert-only", "--out", str(out_bundle)]) == 0
    out = capsys.readouterr().out

    assert out_bundle.exists()
    assert "kaos import" in out
    with zipfile.ZipFile(out_bundle) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["agent_name"] == "obsidian"


def test_import_from_unrecognised_export_reports_clearly(agent, capsys):
    empty = agent / "mystery"
    empty.mkdir()

    assert main(["import-from", "auto", str(empty)]) == 1
    assert "Could not recognise the export" in capsys.readouterr().out


def test_chat_flag_is_ignored_for_non_telegram_importers(agent, capsys):
    vault = agent / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("Заметка.\n", encoding="utf-8")

    assert main(["import-from", "obsidian", str(vault), "--chat", "Work", "--dry-run"]) == 0
    assert "--chat is only supported by the telegram importer" in capsys.readouterr().out


def test_doctor_reports_portability(agent, capsys):
    from kronos.cli import run_doctor

    run_doctor()

    out = capsys.readouterr().out
    assert "Portability" in out
    assert "bundle schema v1" in out
    assert "obsidian" in out
