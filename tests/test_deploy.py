import os
import shutil
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


def _copy_deploy_script_app(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    scripts = app / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "deploy.sh", scripts / "deploy.sh")
    shutil.copy2(ROOT / "scripts" / "_common.sh", scripts / "_common.sh")
    return app


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
            "KAOS_SERVICES": "kaos-main worker",
            "KAOS_MAIN_UNIT": "kaos-main",
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
    assert "Skipping kaos.service install (KAOS_SERVICES does not include kaos; main unit: kaos-main)." in result.stdout
    assert not (systemd_out / "kaos.service").exists()
    installed_health = (systemd_out / "kronos-health.service").read_text(encoding="utf-8")
    assert "After=kaos-main" in installed_health
    assert "After=kaos.service" not in installed_health
    assert f"ExecStart={remote_dir}/app/scripts/health-check.sh" in installed_health
    expected_user = subprocess.run(
        ["whoami"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert f"User={expected_user}" in installed_health
    assert "User=kronos" not in installed_health
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
    assert "Allowed: [A-Za-z0-9/_.-], example: /srv/kaos" in result.stderr
    assert not sudo_log.exists()
    assert not rsync_log.exists()


def test_deploy_uses_common_env_precedence_before_resolving(tmp_path: Path) -> None:
    app = _copy_deploy_script_app(tmp_path)
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    sudo_log = tmp_path / "sudo.log"
    rsync_log = tmp_path / "rsync.log"
    app.joinpath(".env").write_text(
        "\n".join(
            [
                "KAOS_DEPLOY_MODE=remote",
                "KAOS_REMOTE=missing@example.invalid",
                "KAOS_REMOTE_DIR=/opt/bad&unsafe",
                "KAOS_MANAGE_SYSTEMD=true",
            ]
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{_fake_bin(tmp_path)}:{env['PATH']}",
            "KAOS_DEPLOY_MODE": "local",
            "KAOS_REMOTE_DIR": str(remote_dir),
            "KAOS_MANAGE_SYSTEMD": "false",
            "FAKE_SUDO_LOG": str(sudo_log),
            "FAKE_RSYNC_LOG": str(rsync_log),
            "FAKE_PIP_LOG": str(tmp_path / "pip.log"),
            "FAKE_SYSTEMD_DIR": str(tmp_path / "systemd-out"),
        }
    )

    result = subprocess.run(
        ["bash", str(app / "scripts" / "deploy.sh"), "--first-run"],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Target dir: {remote_dir}" in result.stdout
    assert "Manage systemd: false" in result.stdout
    assert "Skipping systemd unit install (KAOS_MANAGE_SYSTEMD=false)." in result.stdout
    assert "systemctl" not in sudo_log.read_text(encoding="utf-8")
    assert "--exclude=.env" in rsync_log.read_text(encoding="utf-8")


def test_deploy_docs_document_renamed_install_contract() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    deployment = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    setup_agents = (ROOT / "scripts" / "setup-agents.sh").read_text(encoding="utf-8")

    assert "KAOS_MAIN_UNIT=kaos" in env_example
    assert "defaults to first KAOS_SERVICES item" in env_example
    assert "KAOS_BACKUP_REPO_DIR=" in env_example
    assert "KAOS_LOG_DIR=" in env_example
    assert "After=$KAOS_MAIN_UNIT" in deployment
    assert "kaos.service` template is installed only" in deployment
    assert "KAOS_MANAGE_SYSTEMD=false" in deployment
    assert "`KAOS_REMOTE_DIR` must be an absolute path" in deployment
    assert "[A-Za-z0-9/_.-]" in deployment
    assert "copy those templates raw" in deployment.lower()
    assert "cp systemd/kaos@.service" not in deployment
    assert "cp systemd/kaos@.service" not in setup_agents
    assert "KAOS_MANAGE_SYSTEMD=true bash scripts/deploy.sh --first-run" in setup_agents
    assert "deploy root: <install-dir>" in deployment
