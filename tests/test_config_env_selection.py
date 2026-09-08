"""Agent processes must not inherit another agent's dotenv values."""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_module_entrypoint_loads_agent_specific_env(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "AGENT_NAME=kronos\nSESSION_FILE=kronos.session\n",
        encoding="utf-8",
    )
    (tmp_path / ".env.resonant").write_text(
        "AGENT_NAME=resonant\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    for key in ("SESSION_FILE", "KAOS_ENV_FILE", "KRONOS_ENV_FILE"):
        env.pop(key, None)
    env["AGENT_NAME"] = "resonant"
    env["PYTHONPATH"] = os.pathsep.join(part for part in (str(REPO_ROOT), env.get("PYTHONPATH", "")) if part)

    script = """
import json
import os

import kronos.__main__
from kronos.config import _ENV_FILE, settings

print(json.dumps({
    "agent": settings.agent_name,
    "env_file": _ENV_FILE,
    "session_file": os.environ.get("SESSION_FILE"),
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

    result = json.loads(completed.stdout.splitlines()[-1])
    assert result == {
        "agent": "resonant",
        "env_file": ".env.resonant",
        "session_file": None,
    }
