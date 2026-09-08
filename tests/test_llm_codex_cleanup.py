"""Real local process trees, never Codex or a remote model."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kronos.llm_codex import ChatCodexCLI

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Production process-group contract is POSIX")


class LocalProcessCLI(ChatCodexCLI):
    script: str

    def _args(self, prompt: str, output_path: str) -> list[str]:
        return [sys.executable, "-c", self.script, output_path]


def _script(record: Path, *, exit_leader: bool = False) -> str:
    child = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"
    return f"""
import json,os,signal,subprocess,sys,time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-c', {child!r}])
Path({str(record)!r}).write_text(json.dumps({{'parent': os.getpid(), 'child': child.pid, 'output': sys.argv[1]}}))
{'sys.exit(0)' if exit_leader else 'time.sleep(60)'}
"""


def _kill_owned_group(record: Path) -> None:
    if not record.exists():
        return
    try:
        os.killpg(json.loads(record.read_text())["parent"], signal.SIGKILL)
    except ProcessLookupError:
        pass


def _running(pid: int) -> bool:
    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, check=False)
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


async def _wait_for_record(record: Path) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if record.exists():
            try:
                return json.loads(record.read_text())
            except json.JSONDecodeError:
                pass
        await asyncio.sleep(0.01)
    pytest.fail("Local child did not start")


def _assert_stopped(record: Path) -> None:
    data = json.loads(record.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not any(_running(data[k]) for k in ("parent", "child")):
            break
        time.sleep(0.02)
    assert not _running(data["parent"])
    assert not _running(data["child"])
    assert not Path(data["output"]).exists()


@pytest.fixture(autouse=True)
def quick_cleanup(monkeypatch):
    monkeypatch.setattr("kronos.llm_codex._PROCESS_EXIT_GRACE_SECONDS", 0.15)


@pytest.mark.parametrize("exit_leader", [False, True])
async def test_timeout_stops_tree_even_after_parent_exits(tmp_path, exit_leader):
    record = tmp_path / "processes.json"
    model = LocalProcessCLI(script=_script(record, exit_leader=exit_leader), timeout_seconds=1)
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(model._run_async("unused"), 5)
        _assert_stopped(record)
    finally:
        _kill_owned_group(record)


def test_sync_timeout_stops_descendants_and_removes_output(tmp_path):
    record = tmp_path / "processes.json"
    model = LocalProcessCLI(script=_script(record), timeout_seconds=1)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            model._run_sync("unused")
        _assert_stopped(record)
    finally:
        _kill_owned_group(record)


async def test_repeated_cancellation_cannot_interrupt_cleanup_or_kill_unrelated_process(tmp_path):
    record = tmp_path / "processes.json"
    model = LocalProcessCLI(script=_script(record), timeout_seconds=30)
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    task = asyncio.create_task(model._run_async("unused"))
    try:
        await _wait_for_record(record)
        task.cancel()
        await asyncio.sleep(0.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        _assert_stopped(record)
        assert unrelated.poll() is None
    finally:
        _kill_owned_group(record)
        unrelated.kill()
        unrelated.wait()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_cancel_during_launch_keeps_ownership_of_eventual_process(tmp_path, monkeypatch):
    record = tmp_path / "processes.json"
    model = LocalProcessCLI(script=_script(record), timeout_seconds=30)
    real_create = asyncio.create_subprocess_exec
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_create(*args, **kwargs):
        proc = await real_create(*args, **kwargs)
        started.set()
        await release.wait()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_create)
    task = asyncio.create_task(model._run_async("unused"))
    try:
        await asyncio.wait_for(started.wait(), 5)
        await _wait_for_record(record)
        task.cancel()
        await asyncio.sleep(0.03)
        assert not task.done(), "Cancellation must await the process handle and cleanup"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        _assert_stopped(record)
    finally:
        release.set()
        _kill_owned_group(record)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_failed_spawn_preserves_error_and_removes_tempfile(monkeypatch):
    paths = []

    async def failed_create(*args, **kwargs):
        paths.append(Path(args[args.index("--output-last-message") + 1]))
        raise FileNotFoundError("missing test executable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failed_create)
    with pytest.raises(FileNotFoundError, match="missing test executable"):
        await ChatCodexCLI()._run_async("unused")
    assert paths and all(not path.exists() for path in paths)


@pytest.mark.parametrize("mode", ["async", "sync"])
async def test_success_preserves_answer_and_removes_tempfile(tmp_path, mode):
    record = tmp_path / "output-path.txt"
    script = f"from pathlib import Path; import sys; Path({str(record)!r}).write_text(sys.argv[1]); Path(sys.argv[1]).write_text('ok')"
    model = LocalProcessCLI(script=script)
    answer = await model._run_async("unused") if mode == "async" else model._run_sync("unused")
    assert answer == "ok"
    assert not Path(record.read_text()).exists()
