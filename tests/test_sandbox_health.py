"""Proving the sandbox runs code — and still contains the code it runs.

`sandbox_ready()` asks whether Docker and the image exist. Both of this
subsystem's real bugs answered that question perfectly and failed everything
after it: a temp directory at 0700 the container could not read, and a bind
mount passed relatively, which Docker takes for a named volume. The sandbox
called itself ready and every single run failed.

So the probe runs code. And because a sandbox that runs without containing is
worse than one that will not start, it also asks the container what it can see
and touch. These tests pin the *reporting* of that — the container's real
answers were measured against live Docker, which no test here can do.
"""

import pytest

from kronos.health import STATUS_BROKEN, STATUS_OFF, STATUS_OK
from kronos.tools import sandbox
from kronos.tools.sandbox import (
    CHECK_DOCKER,
    CHECK_EXECUTION,
    CHECK_IMAGE,
    CHECK_NO_NETWORK,
    CHECK_NON_ROOT,
    CHECK_READONLY_ROOT,
    CHECK_WORKSPACE,
    CONTAINMENT_CHECKS,
    PROBE_MARKER,
    check_sandbox_health,
)

# What the container really answered on a live host, measured before this was
# written: uid 1001, loopback only, root refusing a write.
REAL_REPORT = f'{{"marker": "{PROBE_MARKER}", "uid": 1001, "interfaces": ["lo"], "root_writable": false}}'


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    files = tmp_path / "sandbox" / "health-check" / "files"
    monkeypatch.setattr(sandbox, "_probe_workspace", lambda: files)
    return files


@pytest.fixture
def docker(monkeypatch):
    monkeypatch.setattr(sandbox, "_docker_available", lambda: True)
    monkeypatch.setattr(sandbox, "_docker_image_available", lambda *a, **kw: True)


def container(monkeypatch, *, stdout=REAL_REPORT, stderr="", writes_file=True):
    """Stand in for a container: the probe run, then the workspace run."""

    async def _run(code, timeout=None, memory_limit=None, network=False, files_dir=None, storage_mb=None):
        if files_dir is None:
            return stdout, stderr
        if writes_file:
            from pathlib import Path

            (Path(files_dir) / f"{PROBE_MARKER}.txt").write_text("ok")
            return "wrote", ""
        return "", "docker: invalid characters for a local volume name"

    monkeypatch.setattr(sandbox, "execute_sandboxed", _run)


def by_name(checks):
    return {c.name: c for c in checks}


# --- what is missing rather than broken ---------------------------------------


@pytest.mark.asyncio
async def test_a_host_without_docker_is_off_not_broken(monkeypatch):
    """Opting out of running code is a choice, and alerting about it is noise."""
    monkeypatch.setattr(sandbox, "_docker_available", lambda: False)

    checks = await check_sandbox_health()

    assert [c.name for c in checks] == [CHECK_DOCKER]
    assert checks[0].status == STATUS_OFF


@pytest.mark.asyncio
async def test_a_missing_image_on_a_docker_host_is_broken(monkeypatch):
    """deploy.sh builds it whenever docker exists, so its absence is drift."""
    monkeypatch.setattr(sandbox, "_docker_available", lambda: True)
    monkeypatch.setattr(sandbox, "_docker_image_available", lambda *a, **kw: False)

    checks = by_name(await check_sandbox_health())

    assert checks[CHECK_IMAGE].status == STATUS_BROKEN
    assert "build-sandbox.sh" in checks[CHECK_IMAGE].detail
    assert CHECK_EXECUTION not in checks, "nothing can be run, so nothing further is claimed"


# --- running code is the check a readiness flag cannot be --------------------


@pytest.mark.asyncio
async def test_a_working_sandbox_reports_every_check(docker, workspace, monkeypatch):
    container(monkeypatch)

    checks = by_name(await check_sandbox_health())

    assert {c: checks[c].status for c in checks} == {
        CHECK_DOCKER: STATUS_OK,
        CHECK_IMAGE: STATUS_OK,
        CHECK_EXECUTION: STATUS_OK,
        CHECK_NO_NETWORK: STATUS_OK,
        CHECK_READONLY_ROOT: STATUS_OK,
        CHECK_NON_ROOT: STATUS_OK,
        CHECK_WORKSPACE: STATUS_OK,
    }


@pytest.mark.asyncio
async def test_the_permission_failure_is_caught(docker, workspace, monkeypatch):
    """The real bug: ready by every configuration measure, dead on every run."""
    container(monkeypatch, stdout="", stderr="Error: [Errno 13] Permission denied: '/code/tool.py'")

    checks = by_name(await check_sandbox_health())

    assert checks[CHECK_EXECUTION].status == STATUS_BROKEN
    assert "Permission denied" in checks[CHECK_EXECUTION].detail


@pytest.mark.asyncio
async def test_the_relative_mount_failure_is_caught(docker, workspace, monkeypatch):
    """The other real bug: only reachable with a session directory attached."""
    container(monkeypatch, writes_file=False)

    checks = by_name(await check_sandbox_health())

    assert checks[CHECK_EXECUTION].status == STATUS_OK, "code without files ran fine — that was the trap"
    assert checks[CHECK_WORKSPACE].status == STATUS_BROKEN
    assert "local volume name" in checks[CHECK_WORKSPACE].detail


@pytest.mark.asyncio
async def test_containment_is_not_claimed_when_nothing_ran(docker, workspace, monkeypatch):
    """Absent, not broken: nothing was measured, so nothing is asserted.

    Reporting them broken would be a guess and reporting them ok a lie. Leaving
    them out is how the shared reporter learns to say "undetermined".
    """
    container(monkeypatch, stdout="", stderr="Error: something")

    checks = by_name(await check_sandbox_health())

    assert CONTAINMENT_CHECKS.isdisjoint(checks)
    assert CHECK_WORKSPACE not in checks


@pytest.mark.asyncio
async def test_output_that_is_not_this_probe_is_not_trusted(docker, workspace, monkeypatch):
    """A container answering with somebody else's JSON has not proved anything."""
    container(monkeypatch, stdout='{"ok": true}')

    checks = by_name(await check_sandbox_health())

    assert checks[CHECK_EXECUTION].status == STATUS_BROKEN


@pytest.mark.asyncio
async def test_chatter_before_the_report_does_not_break_parsing(docker, workspace, monkeypatch):
    """Something logging a line first should not read as a broken sandbox."""
    container(monkeypatch, stdout=f"a warning from somewhere\n{REAL_REPORT}")

    checks = by_name(await check_sandbox_health())

    assert checks[CHECK_EXECUTION].status == STATUS_OK


# --- the walls -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_container_with_a_network_is_reported(docker, workspace, monkeypatch):
    """--network=none quietly not applying is a sandbox that reaches the internet."""
    container(
        monkeypatch,
        stdout=f'{{"marker": "{PROBE_MARKER}", "uid": 1001, "interfaces": ["eth0", "lo"], "root_writable": false}}',
    )

    checks = by_name(await check_sandbox_health())

    assert checks[CHECK_NO_NETWORK].status == STATUS_BROKEN
    assert "eth0" in checks[CHECK_NO_NETWORK].detail


@pytest.mark.asyncio
async def test_a_writable_root_is_reported(docker, workspace, monkeypatch):
    container(
        monkeypatch, stdout=f'{{"marker": "{PROBE_MARKER}", "uid": 1001, "interfaces": ["lo"], "root_writable": true}}'
    )

    checks = by_name(await check_sandbox_health())

    assert checks[CHECK_READONLY_ROOT].status == STATUS_BROKEN


@pytest.mark.asyncio
async def test_running_as_root_is_reported(docker, workspace, monkeypatch):
    container(
        monkeypatch, stdout=f'{{"marker": "{PROBE_MARKER}", "uid": 0, "interfaces": ["lo"], "root_writable": false}}'
    )

    checks = by_name(await check_sandbox_health())

    assert checks[CHECK_NON_ROOT].status == STATUS_BROKEN


@pytest.mark.asyncio
async def test_unreadable_interfaces_are_not_taken_for_containment(docker, workspace, monkeypatch):
    """Failing to see the interfaces is not evidence there are none."""
    container(
        monkeypatch,
        stdout=f'{{"marker": "{PROBE_MARKER}", "uid": 1001, "interfaces": null, "interfaces_error": "no /sys", "root_writable": false}}',
    )

    checks = by_name(await check_sandbox_health())

    assert checks[CHECK_NO_NETWORK].status == STATUS_BROKEN
    assert "no /sys" in checks[CHECK_NO_NETWORK].detail


# --- housekeeping ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_probe_leaves_no_file_behind(docker, workspace, monkeypatch):
    """A daily job dropping a marker into a session directory is litter."""
    container(monkeypatch)

    await check_sandbox_health()

    assert not (workspace / f"{PROBE_MARKER}.txt").exists()


@pytest.mark.asyncio
async def test_a_stale_marker_cannot_fake_a_pass(docker, workspace, monkeypatch):
    """Otherwise a broken mount reads as working, forever, on one leftover file."""
    workspace.mkdir(parents=True)
    (workspace / f"{PROBE_MARKER}.txt").write_text("left over from last time")
    container(monkeypatch, writes_file=False)

    checks = by_name(await check_sandbox_health())

    assert checks[CHECK_WORKSPACE].status == STATUS_BROKEN
