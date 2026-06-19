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


def _init_git_repo(path: Path, remote_url: str = "git@github.com:private/kaos-workspace-backup.git") -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "remote", "add", "origin", remote_url],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_bare_remote(path: Path) -> None:
    subprocess.run(["git", "init", "--bare", str(path)], check=True, capture_output=True, text=True)


def _clean_backup_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "KAOS_WORKSPACE_SRC",
        "KAOS_REPO_DIR",
        "KAOS_BACKUP_REPO_DIR",
        "KAOS_BACKUP_REMOTE",
        "KAOS_BACKUP_BRANCH",
    ):
        env.pop(name, None)
    return env


def _create_workspace(path: Path) -> None:
    for dirname in ("self", "notes", "ops"):
        directory = path / dirname
        directory.mkdir(parents=True)
        (directory / "README.md").write_text(f"# {dirname}\n", encoding="utf-8")


def test_workspace_backup_requires_explicit_source(tmp_path: Path) -> None:
    app = _copy_backup_script_app(tmp_path)
    backup_repo = tmp_path / "backup"
    _init_git_repo(backup_repo)
    env = _clean_backup_env()
    env["KAOS_BACKUP_REPO_DIR"] = str(backup_repo)

    result = subprocess.run(
        ["bash", str(app / "scripts" / "workspace-backup.sh")],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "KAOS_WORKSPACE_SRC is required" in result.stderr
    assert not (app / "workspace").exists()


def test_workspace_backup_requires_explicit_private_destination(tmp_path: Path) -> None:
    app = _copy_backup_script_app(tmp_path)
    workspace = tmp_path / "private-workspace"
    _create_workspace(workspace)
    env = _clean_backup_env()
    env["KAOS_WORKSPACE_SRC"] = str(workspace)

    result = subprocess.run(
        ["bash", str(app / "scripts" / "workspace-backup.sh")],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "KAOS_BACKUP_REPO_DIR is required" in result.stderr
    assert not (app / "workspace").exists()


def test_workspace_backup_refuses_public_app_repo_target(tmp_path: Path) -> None:
    app = _copy_backup_script_app(tmp_path)
    workspace = tmp_path / "private-workspace"
    _create_workspace(workspace)
    env = _clean_backup_env()
    env.update(
        {
            "KAOS_WORKSPACE_SRC": str(workspace),
            "KAOS_BACKUP_REPO_DIR": str(app),
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
    status = subprocess.run(
        ["git", "status", "--short", "--", "workspace"],
        cwd=app,
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""


def test_workspace_backup_refuses_aggregate_app_workspaces_source(tmp_path: Path) -> None:
    app = _copy_backup_script_app(tmp_path)
    backup_repo = tmp_path / "backup"
    _init_git_repo(backup_repo)
    env = _clean_backup_env()
    env.update(
        {
            "KAOS_WORKSPACE_SRC": str(app / "workspaces"),
            "KAOS_BACKUP_REPO_DIR": str(backup_repo),
        }
    )
    (app / "workspaces").mkdir()

    result = subprocess.run(
        ["bash", str(app / "scripts" / "workspace-backup.sh")],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "not the aggregate app/workspaces directory" in result.stderr
    assert not (backup_repo / "workspace").exists()


def test_workspace_backup_refuses_ignored_backup_target_without_staging(tmp_path: Path) -> None:
    app = _copy_backup_script_app(tmp_path)
    workspace = tmp_path / "private-workspace"
    backup_repo = tmp_path / "backup"
    _create_workspace(workspace)
    _init_git_repo(backup_repo)
    (backup_repo / ".gitignore").write_text("workspace/\n", encoding="utf-8")
    env = _clean_backup_env()
    env.update(
        {
            "KAOS_WORKSPACE_SRC": str(workspace),
            "KAOS_BACKUP_REPO_DIR": str(backup_repo),
        }
    )

    result = subprocess.run(
        ["bash", str(app / "scripts" / "workspace-backup.sh")],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
    )

    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=backup_repo,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "backup target workspace/ is ignored" in result.stderr
    assert not (backup_repo / "workspace").exists()
    assert staged.stdout == ""


def test_workspace_backup_refuses_public_code_remote(tmp_path: Path) -> None:
    app = _copy_backup_script_app(tmp_path)
    workspace = tmp_path / "private-workspace"
    backup_repo = tmp_path / "backup"
    _create_workspace(workspace)
    _init_git_repo(backup_repo, remote_url="https://github.com/spyrae/kronos-agent-os.git")
    env = _clean_backup_env()
    env.update(
        {
            "KAOS_WORKSPACE_SRC": str(workspace),
            "KAOS_BACKUP_REPO_DIR": str(backup_repo),
        }
    )

    result = subprocess.run(
        ["bash", str(app / "scripts" / "workspace-backup.sh"), "--dry-run"],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "backup remote points to public code repo" in result.stderr
    assert not (backup_repo / "workspace").exists()


def test_workspace_backup_dry_run_prints_plan_without_writing(tmp_path: Path) -> None:
    app = _copy_backup_script_app(tmp_path)
    workspace = tmp_path / "private-workspace"
    backup_repo = tmp_path / "backup"
    remote_repo = tmp_path / "remote.git"
    _create_workspace(workspace)
    _init_bare_remote(remote_repo)
    _init_git_repo(backup_repo, remote_url=str(remote_repo))
    env = _clean_backup_env()
    env.update(
        {
            "KAOS_WORKSPACE_SRC": str(workspace),
            "KAOS_BACKUP_REPO_DIR": str(backup_repo),
            "KAOS_BACKUP_REMOTE": "origin",
            "KAOS_BACKUP_BRANCH": "main",
        }
    )

    result = subprocess.run(
        ["bash", str(app / "scripts" / "workspace-backup.sh"), "--dry-run"],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Backup preflight:" in result.stdout
    assert "Dry run complete. No rsync, staging, commit, or push performed." in result.stdout
    assert str(remote_repo) in result.stdout
    assert not (backup_repo / "workspace").exists()
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=backup_repo,
        check=True,
        capture_output=True,
        text=True,
    )
    refs = subprocess.run(
        ["git", "--git-dir", str(remote_repo), "show-ref"],
        capture_output=True,
        text=True,
    )
    assert staged.stdout == ""
    assert refs.returncode == 1
    assert refs.stdout == ""
