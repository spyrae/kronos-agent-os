"""`kaos repos` — the only way a directory becomes readable by the agent.

Registering is the moment the boundary is drawn, so what this command says
matters: an owner should finish it knowing that the access is read-only and
that credentials are refused.
"""

import json

import pytest

from kronos import repos
from kronos.cli import main
from kronos.config import settings


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "data" / "session.db"))
    import kronos.db as _db

    _db._instances.clear()
    yield
    _db._instances.clear()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text("x = 1\n")
    return root


def test_listing_nothing_says_how_to_add(capsys):
    assert main(["repos", "list"]) == 0

    assert "kaos repos add" in capsys.readouterr().out


def test_adding_a_repository_states_what_it_allows(project, capsys):
    assert main(["repos", "add", "project", str(project)]) == 0

    out = capsys.readouterr().out
    assert "read-only" in out
    assert "Credentials" in out, "the owner should learn the boundary at the moment they draw it"
    assert repos.get_repo("project").path == str(project.resolve())


def test_adding_a_path_that_is_not_a_directory_fails(tmp_path, capsys):
    (tmp_path / "file.txt").write_text("x")

    assert main(["repos", "add", "bad", str(tmp_path / "file.txt")]) == 1
    assert "not a directory" in capsys.readouterr().out


def test_listing_shows_the_path_and_notes(project, capsys):
    main(["repos", "add", "project", str(project), "--notes", "the agent itself"])
    capsys.readouterr()

    assert main(["repos", "list"]) == 0

    out = capsys.readouterr().out
    assert str(project.resolve()) in out
    assert "the agent itself" in out


def test_a_repository_that_moved_is_flagged(project, capsys):
    main(["repos", "add", "project", str(project)])
    capsys.readouterr()
    project.rename(project.parent / "moved")

    main(["repos", "list"])

    assert "[MISSING]" in capsys.readouterr().out, "a path that stopped existing must not read as fine"


def test_list_json_is_machine_readable(project, capsys):
    main(["repos", "add", "project", str(project)])
    capsys.readouterr()

    assert main(["repos", "list", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["name"] == "project"
    assert payload[0]["permission"] == "read"


def test_removing_says_the_files_are_untouched(project, capsys):
    main(["repos", "add", "project", str(project)])
    capsys.readouterr()

    assert main(["repos", "remove", "project"]) == 0

    assert "untouched" in capsys.readouterr().out
    assert repos.list_repos() == []
    assert project.is_dir(), "removing a registration must not remove a directory"


def test_removing_something_unregistered_fails(capsys):
    assert main(["repos", "remove", "ghost"]) == 1
