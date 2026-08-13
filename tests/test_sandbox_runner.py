"""The container command and the disk watchdog.

A read-write bind mount has no size of its own, so a declared storage budget is
either enforced from outside or it is decoration. These check that it is the
first — and that the command really carries the mounts and the lockdown flags
rather than being assembled and ignored.
"""

import asyncio

import pytest

from kronos.tools import sandbox


def test_the_command_locks_the_container_down():
    command = sandbox.build_sandbox_command("/tmp/code")

    assert "--network=none" in command
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "--security-opt=no-new-privileges" in command
    assert "--user=10001:10001" in command
    assert "--pids-limit=50" in command
    assert "-v" in command and "/tmp/code:/code:ro" in command


def test_without_a_session_directory_nothing_is_writable():
    command = sandbox.build_sandbox_command("/tmp/code")

    assert not any(part.endswith(":/work:rw") for part in command)
    assert "--workdir=/code" in command


def test_with_a_session_directory_it_is_mounted_and_becomes_the_workdir():
    """Model-written code does open("out.csv", "w"); that has to land somewhere real."""
    command = sandbox.build_sandbox_command("/tmp/code", files_dir="/data/sandbox/bali/files")

    assert "/data/sandbox/bali/files:/work:rw" in command
    assert "--workdir=/work" in command


def test_the_network_is_opt_in_per_run():
    assert "--network=bridge" in sandbox.build_sandbox_command("/tmp/c", network=True)


class FakeProc:
    """A container process that never exits on its own."""

    def __init__(self):
        self.returncode = None
        self.killed = False

    def kill(self):
        self.killed = True
        self.returncode = -9


async def test_a_run_that_fills_the_directory_is_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "STORAGE_CHECK_INTERVAL_SECONDS", 0.01)
    (tmp_path / "big.bin").write_bytes(b"x" * 2_100_000)
    proc = FakeProc()

    reason = await sandbox._kill_over_budget(str(tmp_path), limit_mb=2, proc=proc)

    assert proc.killed is True
    assert "more than 2 MB" in reason


async def test_a_run_within_budget_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "STORAGE_CHECK_INTERVAL_SECONDS", 0.01)
    (tmp_path / "small.txt").write_text("x")
    proc = FakeProc()

    async def finish_soon():
        await asyncio.sleep(0.05)
        proc.returncode = 0

    asyncio.create_task(finish_soon())
    reason = await sandbox._kill_over_budget(str(tmp_path), limit_mb=64, proc=proc)

    assert proc.killed is False
    assert reason == ""


async def test_an_unready_sandbox_refuses_before_touching_docker(monkeypatch):
    monkeypatch.setattr(sandbox, "sandbox_ready", lambda: False)
    monkeypatch.setattr(
        sandbox,
        "sandbox_unavailable_message",
        lambda: "Sandbox image kronos-sandbox:latest is missing. Run `scripts/build-sandbox.sh`.",
    )

    stdout, stderr = await sandbox.execute_sandboxed("print(1)")

    assert stdout == ""
    assert "build-sandbox.sh" in stderr


def test_there_is_no_in_process_execution_left():
    """The mode that ran model-written code in the agent's own process is gone."""
    assert not hasattr(sandbox, "_exec_in_process")

    from kronos.tools import dynamic

    assert not hasattr(dynamic, "_run_locally_for_dev")


@pytest.mark.parametrize("attribute", ["DEFAULT_STORAGE_MB", "STORAGE_CHECK_INTERVAL_SECONDS"])
def test_the_budget_knobs_exist_and_are_positive(attribute):
    assert getattr(sandbox, attribute) > 0
