import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _copy_backup_script_app(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    scripts = app / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "workspace-backup.sh", scripts / "workspace-backup.sh")
    shutil.copy2(ROOT / ".gitignore", app / ".gitignore")
    subprocess.run(["git", "init"], cwd=app, check=True, capture_output=True, text=True)
    return app


def _clean_backup_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in ("KAOS_WORKSPACE_SRC", "KAOS_REPO_DIR"):
        env.pop(name, None)
    return env


def _create_workspace(path: Path) -> None:
    for dirname in ("self", "notes", "ops"):
        directory = path / dirname
        directory.mkdir(parents=True)
        (directory / "README.md").write_text(f"# {dirname}\n", encoding="utf-8")


def test_workspace_backup_requires_explicit_source(tmp_path: Path) -> None:
    app = _copy_backup_script_app(tmp_path)

    result = subprocess.run(
        ["bash", str(app / "scripts" / "workspace-backup.sh")],
        cwd=app,
        env=_clean_backup_env(),
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "KAOS_WORKSPACE_SRC is required" in result.stderr
    assert not (app / "workspace").exists()


def test_workspace_backup_refuses_public_app_repo_target(tmp_path: Path) -> None:
    app = _copy_backup_script_app(tmp_path)
    workspace = tmp_path / "private-workspace"
    _create_workspace(workspace)
    env = _clean_backup_env()
    env.update(
        {
            "KAOS_WORKSPACE_SRC": str(workspace),
            "KAOS_REPO_DIR": str(app),
        }
    )

    result = subprocess.run(
        ["bash", str(app / "scripts" / "workspace-backup.sh")],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "refusing to back up into the public app repository" in result.stderr
    assert not (app / "workspace").exists()
