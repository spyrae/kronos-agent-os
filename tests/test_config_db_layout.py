"""Every agent gets its own session store, whatever DB_PATH says.

``sessions`` is keyed by ``thread_id`` alone and each write replaces the whole
row, so two agents sharing one file silently overwrite each other's history in
any thread they both see.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kronos.config import _is_legacy_flat_db_path

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "db_path",
    [
        "",
        "./data/kronos.db",
        "data/kronos.db",
        "./data/lacuna.db",
        "./data/some-future-agent.db",
    ],
)
def test_flat_data_db_is_legacy(db_path: str) -> None:
    assert _is_legacy_flat_db_path(db_path)


@pytest.mark.parametrize(
    "db_path",
    [
        "./data/kronos/session.db",
        "data/lacuna/session.db",
        "/srv/kronos/session.db",
        "./var/kronos.db",
    ],
)
def test_explicit_paths_are_kept(db_path: str) -> None:
    assert not _is_legacy_flat_db_path(db_path)


def test_another_agents_flat_db_path_resolves_to_own_directory(tmp_path: Path) -> None:
    """The regression: three ``.env.<agent>`` files carried DB_PATH=./data/kronos.db."""
    (tmp_path / ".env").write_text("AGENT_NAME=kronos\nDB_PATH=./data/kronos.db\n", encoding="utf-8")
    (tmp_path / ".env.lacuna").write_text("AGENT_NAME=lacuna\nDB_PATH=./data/kronos.db\n", encoding="utf-8")

    env = os.environ.copy()
    for key in ("DB_PATH", "DB_DIR", "MEM0_QDRANT_PATH", "KAOS_ENV_FILE", "KRONOS_ENV_FILE"):
        env.pop(key, None)
    env["AGENT_NAME"] = "lacuna"
    env["PYTHONPATH"] = os.pathsep.join(part for part in (str(REPO_ROOT), env.get("PYTHONPATH", "")) if part)

    script = """
import json

from kronos.config import settings

print(json.dumps({
    "agent": settings.agent_name,
    "db_path": settings.db_path,
    "db_dir": settings.db_dir,
    "qdrant": settings.mem0_qdrant_path,
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout.splitlines()[-1]) == {
        "agent": "lacuna",
        "db_path": "./data/lacuna/session.db",
        "db_dir": "./data/lacuna",
        "qdrant": "./data/lacuna/qdrant",
    }


def test_startup_refuses_a_session_db_outside_the_agent_directory(monkeypatch, tmp_path: Path) -> None:
    """An explicit nested path into another agent's directory must not start."""
    from kronos.app import _validate_storage_layout_or_exit
    from kronos.config import settings

    monkeypatch.setattr(settings, "db_dir", str(tmp_path / "lacuna"))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "kronos" / "session.db"))

    with pytest.raises(SystemExit):
        _validate_storage_layout_or_exit()


def test_startup_accepts_the_resolved_per_agent_layout(monkeypatch, tmp_path: Path) -> None:
    from kronos.app import _validate_storage_layout_or_exit
    from kronos.config import settings

    monkeypatch.setattr(settings, "db_dir", str(tmp_path / "lacuna"))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "lacuna" / "session.db"))

    _validate_storage_layout_or_exit()
