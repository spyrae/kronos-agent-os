"""Execution commit, notification commit and transport acceptance are distinct."""

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


def run(mode, directory):
    return subprocess.run(
        [sys.executable, "-m", "tests.helpers.turn_delivery_crash", mode, str(directory)],
        cwd=ROOT,
        env={**os.environ, "KAOS_ENV_FILE": "/dev/null", "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("mode", ["crash-producer", "crash-queued", "crash-sent", "crash-first-ack", "crash-final-ack"])
def test_killed_session_recovers_delivery_without_replaying_a_completed_model(tmp_path, mode):
    killed = run(mode, tmp_path)
    assert killed.returncode == -signal.SIGKILL, killed.stderr
    with sqlite3.connect(tmp_path / "session.db") as db:
        state, content, requested = db.execute(
            "SELECT status, final_content, delivery_requested FROM active_turns"
        ).fetchone()
        queued = db.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0]
        assert requested == 1
        assert (state == "done") == bool(content) == bool(queued) == (mode != "crash-producer")
        if mode == "crash-producer":
            assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    resumed = run("recover", tmp_path)
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout.splitlines()[-1])["state"] == "delivered"
    with sqlite3.connect(tmp_path / "provider.db") as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    models = (tmp_path / "model.log").read_text()
    assert len(models.splitlines()) == (2 if mode == "crash-producer" else 1)
    before = (tmp_path / "requests.log").read_text()
    assert len(before.splitlines()) == (3 if mode == "crash-sent" else 2)
    again = run("recover", tmp_path)
    assert again.returncode == 0, again.stderr
    assert (tmp_path / "requests.log").read_text() == before
    assert (tmp_path / "model.log").read_text() == models
    with sqlite3.connect(tmp_path / "session.db") as db:
        assert db.execute("PRAGMA quick_check").fetchone() == ("ok",)
