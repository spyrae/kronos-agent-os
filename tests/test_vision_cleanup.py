"""Vision uses the same owned process lifecycle as the text Codex adapter."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kronos import vision
from kronos.config import settings
from tests.test_llm_codex_cleanup import _assert_stopped, _kill_owned_group, _wait_for_record

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Production process-group contract is POSIX")


@pytest.fixture
def local_vision(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kaos_vision_provider", "codex-cli")
    monkeypatch.setattr(settings, "kaos_vision_timeout_seconds", 1)
    monkeypatch.setattr(vision.shutil, "which", lambda command: "/test-only/codex")
    monkeypatch.setattr("kronos.llm_codex._PROCESS_EXIT_GRACE_SECONDS", 0.15)
    record = tmp_path / "processes.json"
    real_create = asyncio.create_subprocess_exec
    paths = []

    def intercept(mode="wait", *, started=None, release=None):
        child = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"
        script = f"""
import json,os,signal,subprocess,sys,time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
output, image = sys.argv[1:]
assert Path(image).read_bytes() == b'test-image'
child = subprocess.Popen([sys.executable, '-c', {child!r}])
Path({str(record)!r}).write_text(json.dumps({{'parent':os.getpid(),'child':child.pid,'output':output,'image':image}}))
{"sys.exit(0)" if mode == "exit_leader" else "time.sleep(60)"}
"""
        if mode in {"success", "failure", "empty"}:
            script = (
                "from pathlib import Path; import sys; "
                "assert Path(sys.argv[2]).read_bytes() == b'test-image'; "
                + ("Path(sys.argv[1]).write_text('OCR answer'); " if mode == "success" else "")
                + ("sys.exit(3)" if mode == "failure" else "sys.exit(0)")
            )

        async def create(*args, **kwargs):
            assert kwargs["start_new_session"] is True
            assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
            output = args[args.index("--output-last-message") + 1]
            image = args[args.index("--images") + 1]
            paths.extend([Path(output), Path(image)])
            if mode == "missing":
                raise FileNotFoundError("test-only missing command")
            proc = await real_create(sys.executable, "-c", script, output, image, **kwargs)
            if started is not None:
                started.set()
            if release is not None:
                await release.wait()
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    yield record, paths, intercept
    _kill_owned_group(record)


@pytest.mark.parametrize("mode", ["wait", "exit_leader"])
async def test_timeout_stops_vision_descendants_and_removes_both_files(local_vision, mode):
    record, paths, intercept = local_vision
    intercept(mode)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(vision.analyze_image_bytes(b"test-image"), 5)
    _assert_stopped(record)
    assert len(paths) == 2 and all(not path.exists() for path in paths)


async def test_repeated_cancel_keeps_image_until_owned_cleanup_completes(local_vision):
    record, paths, intercept = local_vision
    intercept()
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    task = asyncio.create_task(vision.analyze_image_bytes(b"test-image"))
    try:
        await _wait_for_record(record)
        task.cancel()
        await asyncio.sleep(0.03)
        assert all(path.exists() for path in paths)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        _assert_stopped(record)
        assert all(not path.exists() for path in paths)
        assert unrelated.poll() is None
    finally:
        unrelated.kill()
        unrelated.wait()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_cancel_during_vision_launch_retains_late_process_handle(local_vision):
    record, paths, intercept = local_vision
    started, release = asyncio.Event(), asyncio.Event()
    intercept(started=started, release=release)
    task = asyncio.create_task(vision.analyze_image_bytes(b"test-image"))
    try:
        await asyncio.wait_for(started.wait(), 5)
        await _wait_for_record(record)
        task.cancel()
        await asyncio.sleep(0.03)
        assert not task.done()
        assert all(path.exists() for path in paths)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        _assert_stopped(record)
        assert all(not path.exists() for path in paths)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("mode", ["success", "failure", "empty", "missing"])
async def test_vision_completion_and_failure_remove_files(local_vision, mode):
    _record, paths, intercept = local_vision
    intercept(mode)
    if mode == "success":
        assert (await vision.analyze_image_bytes(b"test-image")).text == "OCR answer"
    else:
        error = FileNotFoundError if mode == "missing" else RuntimeError
        with pytest.raises(error):
            await vision.analyze_image_bytes(b"test-image")
    assert len(paths) == 2 and all(not path.exists() for path in paths)
