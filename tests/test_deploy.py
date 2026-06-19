import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts" / "deploy.sh"


def _first_run_target_script() -> str:
    text = DEPLOY.read_text(encoding="utf-8")
    first_run = text.index('if [ "${1:-}" = "--first-run" ]; then')
    heredoc = text.index("target_bash <<'TARGET_SCRIPT'", first_run)
    start = text.index("\n", heredoc) + 1
    end = text.index("\nTARGET_SCRIPT", start)
    return text[start:end]


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fake_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "python3",
        """#!/bin/sh
if [ "${1:-}" = "-" ]; then
  cat >/dev/null
  exit 0
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "venv" ]; then
  venv="$3"
  mkdir -p "$venv/bin"
  cat > "$venv/bin/pip" <<'PIP'
#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_PIP_LOG"
exit 0
PIP
  chmod +x "$venv/bin/pip"
  exit 0
fi
exit 0
""",
    )
    _write_executable(
        bin_dir / "sudo",
        """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_SUDO_LOG"
exit 0
""",
    )
    return bin_dir


def test_deploy_first_run_skips_systemd_when_manage_systemd_false(tmp_path: Path) -> None:
    remote_dir = tmp_path / "remote"
    (remote_dir / "app" / "systemd").mkdir(parents=True)
    (remote_dir / "app" / "systemd" / "kaos.service").write_text(
        "[Service]\nUser=kronos\nExecStart=/opt/kaos/app/.venv/bin/python -m kronos\n",
        encoding="utf-8",
    )
    sudo_log = tmp_path / "sudo.log"
    pip_log = tmp_path / "pip.log"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{_fake_bin(tmp_path)}:{env['PATH']}",
            "KAOS_REMOTE_DIR": str(remote_dir),
            "KAOS_MANAGE_SYSTEMD": "false",
            "FAKE_SUDO_LOG": str(sudo_log),
            "FAKE_PIP_LOG": str(pip_log),
        }
    )

    result = subprocess.run(
        ["bash"],
        input=_first_run_target_script(),
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Skipping systemd unit install (KAOS_MANAGE_SYSTEMD=false)." in result.stdout
    assert not sudo_log.exists()
    assert 'install -e app/.[dev]' in pip_log.read_text(encoding="utf-8")
    assert "install edge-tts" in pip_log.read_text(encoding="utf-8")


def test_deploy_first_run_and_update_share_systemd_management_contract() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    first_run_script = _first_run_target_script()

    assert text.count('if [ "${KAOS_MANAGE_SYSTEMD:-true}" = "true" ]; then') == 2
    assert text.count("Skipping systemd unit install (KAOS_MANAGE_SYSTEMD=false).") == 2
    assert '[ -f "$f" ] && sudo sed' in first_run_script
