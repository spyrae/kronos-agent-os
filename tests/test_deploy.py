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
case "${1:-}" in
  sed)
    shift
    command sed "$@"
    ;;
  tee)
    shift
    target="$1"
    mkdir -p "$FAKE_SYSTEMD_DIR"
    cat > "$FAKE_SYSTEMD_DIR/$(basename "$target")"
    ;;
  systemctl)
    exit 0
    ;;
esac
exit 0
""",
    )
    _write_executable(
        bin_dir / "rsync",
        """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_RSYNC_LOG"
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
            "FAKE_SYSTEMD_DIR": str(tmp_path / "systemd-out"),
            "FAKE_RSYNC_LOG": str(tmp_path / "rsync.log"),
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
    assert '[ -f "$f" ] || continue' in first_run_script


def test_deploy_rewrites_ops_after_and_skips_generic_main_on_renamed_install(tmp_path: Path) -> None:
    remote_dir = tmp_path / "remote"
    systemd_dir = remote_dir / "app" / "systemd"
    systemd_dir.mkdir(parents=True)
    (systemd_dir / "kaos.service").write_text(
        "[Unit]\nDescription=Generic main\n[Service]\nUser=kronos\nExecStart=/opt/kaos/app/.venv/bin/python -m kronos\n",
        encoding="utf-8",
    )
    (systemd_dir / "kronos-health.service").write_text(
        "[Unit]\nAfter=kaos.service\n[Service]\nUser=kronos\nExecStart=/opt/kaos/app/scripts/health-check.sh\n",
        encoding="utf-8",
    )
    sudo_log = tmp_path / "sudo.log"
    systemd_out = tmp_path / "systemd-out"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{_fake_bin(tmp_path)}:{env['PATH']}",
            "KAOS_REMOTE_DIR": str(remote_dir),
            "KAOS_AGENTS": "kronos",
            "KAOS_SERVICES": "kronos-ii impulse",
            "KAOS_MAIN_UNIT": "kronos-ii",
            "KAOS_MANAGE_SYSTEMD": "true",
            "FAKE_SUDO_LOG": str(sudo_log),
            "FAKE_PIP_LOG": str(tmp_path / "pip.log"),
            "FAKE_SYSTEMD_DIR": str(systemd_out),
            "FAKE_RSYNC_LOG": str(tmp_path / "rsync.log"),
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
    assert "Skipping kaos.service install (KAOS_SERVICES does not include kaos; main unit: kronos-ii)." in result.stdout
    assert not (systemd_out / "kaos.service").exists()
    installed_health = (systemd_out / "kronos-health.service").read_text(encoding="utf-8")
    assert "After=kronos-ii" in installed_health
    assert "After=kaos.service" not in installed_health
    assert f"ExecStart={remote_dir}/app/scripts/health-check.sh" in installed_health
    assert "systemctl daemon-reload" in sudo_log.read_text(encoding="utf-8")


def test_deploy_rejects_unsafe_remote_dir_before_sync_or_systemd(tmp_path: Path) -> None:
    sudo_log = tmp_path / "sudo.log"
    rsync_log = tmp_path / "rsync.log"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{_fake_bin(tmp_path)}:{env['PATH']}",
            "KAOS_DEPLOY_MODE": "local",
            "KAOS_REMOTE_DIR": "/opt/foo&bar",
            "FAKE_SUDO_LOG": str(sudo_log),
            "FAKE_RSYNC_LOG": str(rsync_log),
            "FAKE_PIP_LOG": str(tmp_path / "pip.log"),
            "FAKE_SYSTEMD_DIR": str(tmp_path / "systemd-out"),
        }
    )

    result = subprocess.run(
        ["bash", str(DEPLOY), "--first-run"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "KAOS_REMOTE_DIR contains unsafe characters" in result.stderr
    assert "Allowed: [A-Za-z0-9/_.-], example: /opt/kronos-ii" in result.stderr
    assert not sudo_log.exists()
    assert not rsync_log.exists()


def test_deploy_docs_document_renamed_install_contract() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    deployment = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")

    assert "KAOS_MAIN_UNIT=kronos-ii" in env_example
    assert "defaults to first KAOS_SERVICES item" in env_example
    assert "After=$KAOS_MAIN_UNIT" in deployment
    assert "kaos.service` template is installed only" in deployment
    assert "KAOS_MANAGE_SYSTEMD=false" in deployment
    assert "`KAOS_REMOTE_DIR` must be an absolute path" in deployment
    assert "[A-Za-z0-9/_.-]" in deployment
