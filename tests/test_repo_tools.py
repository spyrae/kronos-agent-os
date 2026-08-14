"""Reading a repository through the tools, against a real git repository.

Real, because the interesting failures are git's: a search that matched nothing
exits non-zero, a diff of an untracked tree is empty, a ref that does not exist
is an error rather than a silence. Faking git would test the fake.
"""

import subprocess

import pytest

from kronos import repos
from kronos.config import settings
from kronos.tools.repo_tools import (
    REPO_TOOLS,
    repo_diff,
    repo_history,
    repo_list,
    repo_read,
    repo_search,
    repo_tree,
)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "data" / "session.db"))
    import kronos.db as _db

    _db._instances.clear()
    yield
    _db._instances.clear()


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("def main():\n    return 'hello'\n\n\ndef helper():\n    return 42\n")
    (root / "README.md").write_text("# Project\n")
    (root / ".env").write_text("API_KEY=sk-live-must-not-appear\n")
    (root / ".gitignore").write_text(".env\n")

    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first commit")
    return repos.add_repo("project", str(root))


# --- listing ------------------------------------------------------------------


async def test_listing_with_nothing_registered_says_how_to_add_one():
    assert "kaos repos add" in await repo_list.ainvoke({})


async def test_registered_repositories_are_listed(repo):
    assert "project" in await repo_list.ainvoke({})


async def test_the_tree_shows_source_and_not_secrets(repo):
    out = await repo_tree.ainvoke({"repo": "project"})

    assert "src/main.py" in out
    assert ".env" not in out


async def test_an_unknown_repository_is_an_error(repo):
    assert (await repo_tree.ainvoke({"repo": "trackvibe"})).startswith("[ERROR]")


# --- reading ------------------------------------------------------------------


async def test_a_file_comes_back_with_line_numbers(repo):
    out = await repo_read.ainvoke({"repo": "project", "path": "src/main.py"})

    assert "def main" in out
    assert "    1  " in out, "line numbers make a follow-up range possible"
    assert "lines 1-6 of 6" in out


async def test_a_range_can_be_asked_for(repo):
    out = await repo_read.ainvoke({"repo": "project", "path": "src/main.py", "start": 5, "end": 6})

    assert "def helper" in out
    assert "def main" not in out


async def test_reading_past_the_end_says_so(repo):
    out = await repo_read.ainvoke({"repo": "project", "path": "src/main.py", "start": 999})

    assert out.startswith("[ERROR]")
    assert "past the end" in out


async def test_reading_a_secret_is_refused(repo):
    """The file exists and is readable on disk; the boundary is what stops it."""
    out = await repo_read.ainvoke({"repo": "project", "path": ".env"})

    assert out.startswith("[ERROR]")
    assert "sk-live-must-not-appear" not in out


async def test_escaping_the_repository_is_refused(repo):
    out = await repo_read.ainvoke({"repo": "project", "path": "../../etc/passwd"})

    assert out.startswith("[ERROR]")
    assert "outside repository" in out


# --- searching ----------------------------------------------------------------


async def test_a_search_returns_the_matching_lines(repo):
    out = await repo_search.ainvoke({"repo": "project", "pattern": "def "})

    assert "src/main.py" in out
    assert "def helper" in out


async def test_a_search_that_matches_nothing_is_an_answer_not_a_failure(repo):
    """git grep exits 1 when nothing matched; that is not an error to report."""
    out = await repo_search.ainvoke({"repo": "project", "pattern": "zzz-not-here"})

    assert not out.startswith("[ERROR]")
    assert "No match" in out


async def test_a_search_never_surfaces_a_line_from_a_secret_file(repo):
    out = await repo_search.ainvoke({"repo": "project", "pattern": "API_KEY"})

    assert "sk-live-must-not-appear" not in out


async def test_searching_outside_the_repository_is_refused(repo):
    assert (await repo_search.ainvoke({"repo": "project", "pattern": "x", "path": "../"})).startswith("[ERROR]")


# --- history and diff ---------------------------------------------------------


async def test_history_shows_recent_commits(repo):
    out = await repo_history.ainvoke({"repo": "project"})

    assert "first commit" in out


async def test_history_can_follow_one_path(repo):
    out = await repo_history.ainvoke({"repo": "project", "path": "src/main.py"})

    assert "first commit" in out


async def test_a_clean_tree_reports_no_changes(repo):
    assert "Nothing changed" in await repo_diff.ainvoke({"repo": "project"})


async def test_uncommitted_work_shows_up_in_the_diff(repo):
    (repo.root / "src" / "main.py").write_text("def main():\n    return 'goodbye'\n")

    out = await repo_diff.ainvoke({"repo": "project"})

    assert "goodbye" in out
    assert "src/main.py" in out


async def test_a_bad_ref_is_reported_rather_than_swallowed(repo):
    out = await repo_diff.ainvoke({"repo": "project", "ref": "no-such-branch"})

    assert out.startswith("[ERROR]")


async def test_a_large_diff_is_truncated_with_a_way_forward(repo, monkeypatch):
    import kronos.tools.repo_tools as tools

    monkeypatch.setattr(tools, "MAX_DIFF_CHARS", 200)
    (repo.root / "src" / "big.py").write_text("# line\n" * 500)
    _git(repo.root, "add", "-A")

    out = await repo_diff.ainvoke({"repo": "project", "ref": "HEAD"})

    assert "truncated" in out


# --- how the runtime must treat this ------------------------------------------


def test_repository_contents_are_treated_as_untrusted():
    """A comment in a repository is as good a place to hide an instruction as a web page."""
    for tool in REPO_TOOLS:
        assert (tool.metadata or {}).get("untrusted_output") is True, tool.name


def test_nothing_here_writes():
    """v1 reads. A tool that could commit would need a different approval story."""
    assert {tool.name for tool in REPO_TOOLS} == {
        "repo_list",
        "repo_tree",
        "repo_read",
        "repo_search",
        "repo_history",
        "repo_diff",
    }
