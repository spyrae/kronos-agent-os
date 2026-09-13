"""A real process dies at delivery boundaries, then a fresh owner recovers."""

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
        [sys.executable, "-m", "tests.helpers.delivery_crash", mode, str(directory)],
        cwd=ROOT,
        env={**os.environ, "KAOS_ENV_FILE": "/dev/null", "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("mode", ["crash-producer", "crash-queued", "crash-sent", "crash-first-ack", "crash-final-ack"])
def test_crash_keeps_obligation_and_stable_provider_identity(tmp_path, mode):
    killed = run(mode, tmp_path)
    assert killed.returncode == -signal.SIGKILL, killed.stderr
    with sqlite3.connect(tmp_path / "plans.db") as db:
        summary = db.execute("SELECT summary FROM plans").fetchone()[0]
        queued = db.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0]
        assert bool(summary) == bool(queued) == (mode != "crash-producer")
    result = run("recover", tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1])["summary"] == "delivered"
    with sqlite3.connect(tmp_path / "provider.db") as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    before = (tmp_path / "requests.log").read_text()
    again = run("recover", tmp_path)
    assert again.returncode == 0, again.stderr
    assert before == (tmp_path / "requests.log").read_text()
    assert len(before.splitlines()) == (3 if mode == "crash-sent" else 2)
    with sqlite3.connect(tmp_path / "plans.db") as db:
        assert db.execute("PRAGMA quick_check").fetchone() == ("ok",)
