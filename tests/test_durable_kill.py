"""A real SIGKILL, a real restart (moat phase 10 acceptance).

Every other durable test simulates a crash by leaving a turn `running` in the
database. That proves the recovery logic given the expected state; it does not
prove a process dying mid-flight *produces* that state. SQLite's WAL, the
aiosqlite connection and unflushed writes all sit between the two claims.

So this test kills a real process with signal 9 between the side effect and the
end of the turn, then starts a different process to finish it. Marked
`integration` because it spawns processes and is slower than the rest of the
suite; the CI gate runs `-m "not integration"`.

The side effect appends a line to a file, so "did it happen twice" is not a
counter to trust but a second line to see.
"""

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = "tests.helpers.durable_crash"


def _run(mode: str, workdir: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "KAOS_ENV_FILE": "/dev/null"}
    return subprocess.run(
        [sys.executable, "-m", HELPER, mode, str(workdir)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.fixture
def workdir(tmp_path):
    return tmp_path / "kill"


def test_a_killed_turn_is_finished_by_the_next_process(workdir):
    crash = _run("crash", workdir)

    # -9 and nothing else: an orderly exit would mean the process got to clean up,
    # and then this test would be proving something easier than it claims.
    assert crash.returncode == -signal.SIGKILL, crash.stderr[-2000:]
    sent = (workdir / "sent.log").read_text(encoding="utf-8").splitlines()
    assert len(sent) == 1, "the side effect must have happened exactly once before the kill"

    resume = _run("resume", workdir)

    assert resume.returncode == 0, resume.stderr[-2000:]
    assert "finished=1" in resume.stdout
    assert "delivered=1" in resume.stdout, "the user gets the answer they were waiting for"


def test_the_side_effect_does_not_happen_twice(workdir):
    """The ledger is what makes re-execution safe, and this is the test for it."""
    _run("crash", workdir)
    _run("resume", workdir)

    sent = (workdir / "sent.log").read_text(encoding="utf-8").splitlines()

    assert len(sent) == 1, f"the report was sent again on resume: {sent}"


def test_the_database_survives_the_kill(workdir):
    """A killed process leaves the journal readable — no WAL surprises."""
    import sqlite3

    _run("crash", workdir)
    turn_id = (workdir / "crashed.txt").read_text(encoding="utf-8").splitlines()[0]

    with sqlite3.connect(workdir / "session.db") as db:
        db.row_factory = sqlite3.Row
        turn = db.execute("SELECT * FROM active_turns WHERE turn_id = ?", (turn_id,)).fetchone()
        journal = db.execute("SELECT COUNT(*) FROM turn_journal WHERE turn_id = ?", (turn_id,)).fetchone()[0]
        effects = db.execute("SELECT COUNT(*) FROM external_effects WHERE turn_id = ?", (turn_id,)).fetchone()[0]

    assert turn["status"] == "running", "a killed turn is left running, which is what recovery looks for"
    assert journal >= 1, "the tool call was journalled before the kill"
    assert effects == 1, "the effect was recorded before the kill, which is what stops the repeat"


def test_the_turn_is_closed_after_resume(workdir):
    import sqlite3

    _run("crash", workdir)
    turn_id = (workdir / "crashed.txt").read_text(encoding="utf-8").splitlines()[0]
    _run("resume", workdir)

    with sqlite3.connect(workdir / "session.db") as db:
        db.row_factory = sqlite3.Row
        turn = db.execute("SELECT * FROM active_turns WHERE turn_id = ?", (turn_id,)).fetchone()

    assert turn["status"] == "done"
    assert turn["attempts"] == 1, "one crash, one resume attempt"
    assert turn["completed_at"] is not None


def test_a_second_resume_finds_nothing_to_do(workdir):
    """Two processes starting at once must not both finish the same turn."""
    _run("crash", workdir)
    first = _run("resume", workdir)
    second = _run("resume", workdir)

    assert "finished=1" in first.stdout
    assert "finished=0" in second.stdout
    assert len((workdir / "sent.log").read_text(encoding="utf-8").splitlines()) == 1
