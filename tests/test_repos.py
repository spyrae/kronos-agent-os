"""The boundary around a repository, which is the whole of this module.

A directory tree has no edges unless someone draws them. These tests are the
edges: nothing outside a registered root, nothing that looks like a credential,
nothing the repository itself gitignores. Each has a way of being got around —
`../`, a symlink, a secret one directory down — and each is checked.
"""

import pytest

from kronos import repos
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
def repo(tmp_path):
    """A small repository with the shapes that matter in it."""
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "data").mkdir()
    (root / "src" / "main.py").write_text("def main():\n    return 'hello'\n")
    (root / "README.md").write_text("# Project\n\nIt does things.\n")
    (root / ".env").write_text("API_KEY=sk-real-secret\n")
    (root / "config" / ".env.production").write_text("DB_PASSWORD=hunter2\n")
    (root / "config" / "id_rsa").write_text("-----BEGIN PRIVATE KEY-----\n")
    (root / "data" / "notes.txt").write_text("local state\n")
    (root / ".gitignore").write_text("data/\n*.log\n")
    (root / "debug.log").write_text("noisy\n")
    return repos.add_repo("project", str(root))


# --- the registry -------------------------------------------------------------


def test_a_repository_round_trips(repo, tmp_path):
    stored = repos.get_repo("project")

    assert stored.path == str((tmp_path / "project").resolve())
    assert stored.permission == repos.PERMISSION_READ


def test_registering_the_same_name_twice_updates_it(repo, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()

    repos.add_repo("project", str(other))

    assert len(repos.list_repos()) == 1
    assert repos.get_repo("project").path == str(other.resolve())


def test_a_path_that_is_not_a_directory_is_refused(tmp_path):
    (tmp_path / "file.txt").write_text("x")

    with pytest.raises(repos.RepoError, match="not a directory"):
        repos.add_repo("nope", str(tmp_path / "file.txt"))


def test_write_permission_is_refused_while_it_does_not_exist(tmp_path):
    """Accepting a permission nothing honours would be a promise, not a setting."""
    (tmp_path / "r").mkdir()

    with pytest.raises(repos.RepoError, match="does not change them yet"):
        repos.add_repo("r", str(tmp_path / "r"), permission="write")


def test_an_unknown_repository_lists_what_is_registered(repo):
    with pytest.raises(repos.RepoError, match="known: project"):
        repos.get_repo("trackvibe")


def test_removing_a_repository_is_idempotent(repo):
    assert repos.remove_repo("project") is True
    assert repos.remove_repo("project") is False


# --- staying inside the root --------------------------------------------------


@pytest.mark.parametrize(
    "relative",
    ["../outside.txt", "../../etc/passwd", "src/../../escape", "/etc/passwd"],
)
def test_nothing_outside_the_repository_can_be_reached(repo, relative):
    with pytest.raises(repos.RepoError, match="outside repository"):
        repos.resolve(repo, relative)


def test_a_symlink_pointing_out_of_the_tree_is_refused(repo, tmp_path):
    """Resolution happens before comparison, so a link is the same as `../`."""
    secret = tmp_path / "outside.txt"
    secret.write_text("not yours")
    (repo.root / "shortcut").symlink_to(secret)

    with pytest.raises(repos.RepoError, match="outside repository"):
        repos.resolve(repo, "shortcut")


def test_ordinary_paths_resolve(repo):
    assert repos.resolve(repo, "src/main.py").name == "main.py"
    assert repos.resolve(repo).is_dir()


# --- secrets ------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative",
    [
        ".env",
        "config/.env.production",
        "config/id_rsa",
        "certs/key.pem",
        "AuthKey_ABC.p8",
        "keys/service-account-prod.json",
        "kronos.session",
        "data/app.db",
    ],
)
def test_credentials_are_refused_by_name(relative):
    assert repos.looks_secret(relative) is True


@pytest.mark.parametrize("relative", ["src/main.py", "README.md", "docs/environment.md", "src/keyboard.py"])
def test_ordinary_source_is_not_mistaken_for_a_secret(relative):
    assert repos.looks_secret(relative) is False


def test_reading_a_credential_is_refused_with_the_reason(repo):
    with pytest.raises(repos.RepoError, match="looks like a credential"):
        repos.resolve(repo, ".env")

    with pytest.raises(repos.RepoError, match="looks like a credential"):
        repos.resolve(repo, "config/.env.production")


def test_gitignored_paths_are_refused_as_local_state(repo):
    with pytest.raises(repos.RepoError, match="gitignored"):
        repos.resolve(repo, "data/notes.txt")

    with pytest.raises(repos.RepoError, match="gitignored"):
        repos.resolve(repo, "debug.log")


def test_a_repository_without_a_gitignore_still_works(tmp_path):
    root = tmp_path / "bare"
    root.mkdir()
    (root / "main.py").write_text("x = 1\n")
    bare = repos.add_repo("bare", str(root))

    assert repos.resolve(bare, "main.py").is_file()


# --- reading ------------------------------------------------------------------


def test_a_file_can_be_read(repo):
    assert repos.readable_file(repo, "src/main.py").read_text().startswith("def main")


def test_a_directory_is_not_a_file(repo):
    with pytest.raises(repos.RepoError, match="not a file"):
        repos.readable_file(repo, "src")


def test_a_huge_file_is_refused_rather_than_truncated_silently(repo, monkeypatch):
    monkeypatch.setattr(repos, "MAX_FILE_BYTES", 10)

    with pytest.raises(repos.RepoError, match="larger than this reads"):
        repos.readable_file(repo, "README.md")


def test_walking_shows_source_and_hides_the_rest(repo):
    found = repos.walk(repo)

    assert "src/main.py" in found
    assert "README.md" in found
    assert ".env" not in found
    assert "config/.env.production" not in found
    assert "config/id_rsa" not in found
    assert "data/notes.txt" not in found, "gitignored state is not source"
    assert "debug.log" not in found


def test_walking_skips_directories_nobody_wants_read(repo):
    (repo.root / "node_modules" / "left-pad").mkdir(parents=True)
    (repo.root / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1\n")

    assert not any("node_modules" in path for path in repos.walk(repo))


def test_walking_is_bounded(repo):
    for index in range(50):
        (repo.root / "src" / f"file{index}.py").write_text("x = 1\n")

    assert len(repos.walk(repo, limit=10)) == 10
