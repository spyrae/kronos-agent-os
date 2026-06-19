import os
import shutil
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _copy_notion_script_app(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    scripts = app / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "notion-helper.sh", scripts / "notion-helper.sh")
    shutil.copy2(ROOT / "scripts" / "_common.sh", scripts / "_common.sh")
    return app


def _write_fake_curl(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(
        """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_CURL_LOG"
printf '{"ok":true}\\n'
""",
        encoding="utf-8",
    )
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def _clean_notion_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "KAOS_APP_DIR",
        "KAOS_SCRIPT_DIR",
        "KAOS_COMMON_INITIALIZED",
        "NOTION_TOKEN_PERSONAL",
        "NOTION_TOKEN_TEAM",
    ):
        env.pop(name, None)
    env["PATH"] = f"{_write_fake_curl(tmp_path)}:{env['PATH']}"
    env["FAKE_CURL_LOG"] = str(tmp_path / "curl.log")
    return env


def test_notion_helper_uses_env_file_token_when_process_env_missing(
    tmp_path: Path,
) -> None:
    app = _copy_notion_script_app(tmp_path)
    (app / ".env").write_text(
        "NOTION_TOKEN_PERSONAL=file-personal\nNOTION_TOKEN_TEAM=file-team\n",
        encoding="utf-8",
    )
    env = _clean_notion_env(tmp_path)

    result = subprocess.run(
        ["bash", str(app / "scripts" / "notion-helper.sh"), "-w", "team", "databases"],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == '{"ok":true}\n'
    curl_args = Path(env["FAKE_CURL_LOG"]).read_text(encoding="utf-8")
    assert "Authorization: Bearer file-team" in curl_args


def test_notion_helper_process_env_overrides_env_file_token(tmp_path: Path) -> None:
    app = _copy_notion_script_app(tmp_path)
    (app / ".env").write_text(
        "NOTION_TOKEN_PERSONAL=file-personal\nNOTION_TOKEN_TEAM=file-team\n",
        encoding="utf-8",
    )
    env = _clean_notion_env(tmp_path)
    env["NOTION_TOKEN_PERSONAL"] = "process-personal"

    result = subprocess.run(
        ["bash", str(app / "scripts" / "notion-helper.sh"), "search", "roadmap"],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == '{"ok":true}\n'
    curl_args = Path(env["FAKE_CURL_LOG"]).read_text(encoding="utf-8")
    assert "Authorization: Bearer process-personal" in curl_args
    assert "Authorization: Bearer file-personal" not in curl_args
