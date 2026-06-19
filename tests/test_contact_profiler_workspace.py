import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "contact-profiler.py"
CONTACTS_SUFFIX = Path("notes/world/contacts")


def _base_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in [
        "AGENT_NAME",
        "CONTACTS_DIR",
        "DB_DIR",
        "DB_PATH",
        "KAOS_WORKSPACE_SRC",
        "KRONOS_ENV_FILE",
        "WORKSPACE",
        "WORKSPACE_PATH",
    ]:
        env.pop(name, None)
    env["KAOS_ENV_FILE"] = str(tmp_path / "missing.env")
    env["PROFILER_LOG"] = str(tmp_path / "contact-profiler.log")
    return env


def _run_profiler_snippet(
    tmp_path: Path,
    snippet: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = _base_env(tmp_path)
    if extra_env:
        env.update(extra_env)

    code = f"""
import importlib.util

spec = importlib.util.spec_from_file_location("contact_profiler_under_test", {str(SCRIPT)!r})
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

{snippet}
"""
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_default_contacts_dir_uses_runtime_workspace(tmp_path: Path):
    result = _run_profiler_snippet(tmp_path, "print(module.CONTACTS_DIR)")

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == ROOT / "workspaces" / "kronos" / CONTACTS_SUFFIX


def test_workspace_path_override_writes_to_runtime_contacts(tmp_path: Path):
    workspace = tmp_path / "runtime-workspace"
    result = _run_profiler_snippet(
        tmp_path,
        'print(module.save_dossier("@alice", "hello"))',
        {"WORKSPACE_PATH": str(workspace)},
    )

    assert result.returncode == 0, result.stderr
    filepath = Path(result.stdout.strip())
    assert filepath == workspace / CONTACTS_SUFFIX / "alice.md"
    assert filepath.read_text(encoding="utf-8") == "hello"


def test_legacy_workspace_env_is_ignored(tmp_path: Path):
    legacy_workspace = tmp_path / "legacy-workspace"
    result = _run_profiler_snippet(
        tmp_path,
        "print(module.CONTACTS_DIR)",
        {"WORKSPACE": str(legacy_workspace)},
    )

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == ROOT / "workspaces" / "kronos" / CONTACTS_SUFFIX
    assert not legacy_workspace.exists()


def test_rejects_legacy_app_workspace_target(tmp_path: Path):
    result = _run_profiler_snippet(
        tmp_path,
        "print(module.CONTACTS_DIR)",
        {"WORKSPACE_PATH": str(ROOT / "workspace")},
    )

    assert result.returncode != 0
    assert "legacy app/workspace" in result.stderr


def test_help_documents_workspace_env_roles(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        env=_base_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "WORKSPACE_PATH is the runtime workspace root" in result.stdout
    assert "WORKSPACE is deprecated and ignored" in result.stdout
    assert "KAOS_WORKSPACE_SRC is backup-only" in result.stdout
