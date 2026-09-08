"""SIGKILL and a fresh process, not a fabricated abandoned database row."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]


def run(mode, tmp_path):
    return subprocess.run(
        [sys.executable, "-m", "tests.helpers.plan_crash", mode, str(tmp_path)],
        cwd=ROOT,
        env={**os.environ, "KAOS_ENV_FILE": "/dev/null", "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("mode", ["crash-claim", "crash-link", "crash-model", "crash-finish"])
def test_plan_process_loss_reattaches_without_replacement_turn(tmp_path, mode):
    killed = run(mode, tmp_path)
    assert killed.returncode == -signal.SIGKILL, killed.stderr
    with sqlite3.connect(tmp_path / "plans.db") as db:
        before = db.execute("SELECT state, execution_key FROM plan_steps").fetchone()
    assert before[0] == "running" and before[1]
    original_turn = None
    if mode != "crash-claim":
        with sqlite3.connect(tmp_path / "session.db") as db:
            original_turn = db.execute("SELECT turn_id FROM active_turns").fetchone()[0]
    recovered = run("recover", tmp_path)
    assert recovered.returncode == 0, recovered.stderr
    result = json.loads(recovered.stdout.splitlines()[-1])
    assert result["state"] == "done" and result["turn_count"] == 1
    if original_turn:
        assert result["turn_id"] == original_turn
    again = run("recover", tmp_path)
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout.splitlines()[-1]) == result
    calls = (tmp_path / "models.log").read_text().splitlines()
    assert len(calls) == (2 if mode == "crash-model" else 1)
    for filename in ["plans.db", "session.db"]:
        with sqlite3.connect(tmp_path / filename) as db:
            assert db.execute("PRAGMA quick_check").fetchone() == ("ok",)
