import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "health-check.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fake_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "systemctl",
        """#!/bin/sh
cmd="$1"
shift
case "$cmd" in
  is-active)
    unit="$1"
    if [ "$unit" = "kronos-health.timer" ]; then
      state="${FAKE_HEALTH_TIMER_STATE:-active}"
    else
      state="${FAKE_SERVICE_ACTIVE:-active}"
    fi
    printf '%s\\n' "$state"
    [ "$state" = "active" ] && exit 0
    exit 3
    ;;
  show)
    printf '%s\\n' "${FAKE_UNIT_LOAD_STATE:-loaded}"
    ;;
  *)
    exit 1
    ;;
esac
""",
    )
    _write_executable(
        bin_dir / "curl",
        """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_CURL_LOG"
case "$*" in
  *127.0.0.1:8788/health*)
    if [ "${FAKE_BRIDGE_OK:-1}" = "1" ]; then
      printf '{"status":"ok"}\\n'
      exit 0
    fi
    exit 22
    ;;
  *127.0.0.1:8789/api/health*)
    if [ "${FAKE_DASHBOARD_OK:-1}" = "1" ]; then
      printf '{"status":"ok"}\\n'
      exit 0
    fi
    exit 22
    ;;
  *)
    exit 0
    ;;
esac
""",
    )
    _write_executable(
        bin_dir / "df",
        """#!/bin/sh
cat <<'EOF'
Filesystem 1K-blocks Used Available Use% Mounted on
/dev/fake 100000 10000 90000 10% /
EOF
""",
    )
    _write_executable(
        bin_dir / "free",
        """#!/bin/sh
cat <<'EOF'
              total        used        free
Mem:           1000         100         900
EOF
""",
    )
    return bin_dir


def _run_health(
    tmp_path: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    app_dir = tmp_path / "app"
    app_dir.mkdir(exist_ok=True)
    curl_log = tmp_path / "curl.log"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{_fake_bin(tmp_path)}:{env['PATH']}",
            "KAOS_APP_DIR": str(app_dir),
            "KAOS_MAIN_UNIT": "kaos",
            "FAKE_CURL_LOG": str(curl_log),
            "WEBHOOK_SECRET": "",
            "NTFY_TOKEN": "",
        }
    )
    for name in ["REMINDER_WEBHOOK_URL", "NTFY_URL", "NTFY_TOPIC"]:
        env.pop(name, None)
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _curl_log(tmp_path: Path) -> str:
    log_file = tmp_path / "curl.log"
    return log_file.read_text(encoding="utf-8") if log_file.exists() else ""


def test_health_check_active_unit_uses_single_bridge_probe(tmp_path: Path) -> None:
    result = _run_health(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""
    assert _curl_log(tmp_path).count("127.0.0.1:8788/health") == 1


def test_health_check_inactive_loaded_unit_is_failure_even_if_bridge_healthy(tmp_path: Path) -> None:
    result = _run_health(
        tmp_path,
        extra_env={
            "FAKE_SERVICE_ACTIVE": "inactive",
            "FAKE_UNIT_LOAD_STATE": "loaded",
        },
    )

    assert result.returncode == 1
    assert "FAIL: kaos service inactive while bridge is healthy" in result.stdout
    assert "orphan bridge process" in result.stdout
    assert _curl_log(tmp_path).count("127.0.0.1:8788/health") == 1


def test_health_check_misconfigured_unit_is_visible_warning_with_alert(tmp_path: Path) -> None:
    result = _run_health(
        tmp_path,
        "--alert",
        extra_env={
            "FAKE_SERVICE_ACTIVE": "unknown",
            "FAKE_UNIT_LOAD_STATE": "not-found",
            "NTFY_TOKEN": "test-token",
            "NTFY_URL": "https://ntfy.invalid",
            "NTFY_TOPIC": "kaos-test",
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARN: kaos service not found/misconfigured, but bridge is healthy" in result.stdout
    assert "FAIL:" not in result.stdout
    log = _curl_log(tmp_path)
    assert log.count("127.0.0.1:8788/health") == 1
    assert "Priority: low" in log
    assert "https://ntfy.invalid/kaos-test" in log
