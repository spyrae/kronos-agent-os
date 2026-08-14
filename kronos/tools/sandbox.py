"""Docker sandbox for running code the agent wrote.

Everything here runs in a container with no network, a read-only root, every
capability dropped and a non-root uid. **There is no fallback.** An earlier
version dropped to ``exec()`` in the agent's own process when Docker was
missing and ``require_dynamic_tool_sandbox`` was off — a mode described in its
own log line as unsafe, one environment variable away from arbitrary code in
production. Missing Docker now means the code does not run.
"""

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("kronos.tools.sandbox")

SANDBOX_IMAGE = "kronos-sandbox:latest"
SANDBOX_BUILD_SCRIPT = "scripts/build-sandbox.sh"
DEFAULT_TIMEOUT = 30
DEFAULT_MEMORY = "256m"
DEFAULT_STORAGE_MB = 64
STORAGE_CHECK_INTERVAL_SECONDS = 0.5


def _docker_available() -> bool:
    """Check if Docker is available."""
    return shutil.which("docker") is not None


def _docker_image_available(image: str = SANDBOX_IMAGE) -> bool:
    """Check if the sandbox image exists locally."""
    if not _docker_available():
        return False

    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def sandbox_ready() -> bool:
    """Return whether Docker and the local sandbox image are ready."""
    return _docker_available() and _docker_image_available()


def sandbox_status() -> dict[str, object]:
    """Return operator-facing sandbox readiness details."""
    docker_available = _docker_available()
    return {
        "docker_available": docker_available,
        "image": SANDBOX_IMAGE,
        "image_available": _docker_image_available() if docker_available else False,
        "build_script": SANDBOX_BUILD_SCRIPT,
    }


def sandbox_unavailable_message() -> str:
    """Return a concise remediation hint for sandbox setup."""
    status = sandbox_status()
    if not status["docker_available"]:
        return "Docker is required for dynamic tool sandboxing."
    return f"Sandbox image {SANDBOX_IMAGE} is missing. Run `{SANDBOX_BUILD_SCRIPT}`."


SANDBOX_UID = 10001


def container_user() -> str:
    """Which uid:gid the container runs as — never root, and able to use the mounts.

    Bind mounts carry the host's ownership, so a container running as uid 10001
    could neither read a 0700 temp directory nor write into a directory owned by
    the agent's user. Running as the agent's own uid solves both without making
    anything world-accessible; the image's baked-in 10001 stays as the answer for
    the one case where the agent's uid would be root, and there the mounts get
    chowned instead.
    """
    uid = os.getuid() if hasattr(os, "getuid") else SANDBOX_UID
    if uid == 0:
        return f"{SANDBOX_UID}:{SANDBOX_UID}"
    return f"{uid}:{os.getgid()}"


def _grant_access(path: str) -> None:
    """Make a mounted directory usable by the container user."""
    if not hasattr(os, "getuid") or os.getuid() != 0:
        # Running as ourselves: the mounts are already ours.
        return
    try:
        os.chown(path, SANDBOX_UID, SANDBOX_UID)
    except OSError as e:  # pragma: no cover - only reachable as root
        log.warning("Cannot hand %s to the sandbox user: %s", path, e)


def build_sandbox_command(
    tmpdir: str,
    memory_limit: str = DEFAULT_MEMORY,
    network: bool = False,
    files_dir: str | None = None,
) -> list[str]:
    """Build the Docker command for a single sandboxed execution.

    With ``files_dir`` the session's shared directory is mounted read-write at
    /work and becomes the working directory, so ordinary relative writes
    (``open("offers.csv", "w")``) land somewhere that outlives the container.
    Without it nothing is writable but /tmp, which the container discards.
    """
    network_flag = "bridge" if network else "none"
    command = [
        "docker",
        "run",
        "--rm",
        f"--memory={memory_limit}",
        f"--network={network_flag}",
        "--cpus=1",
        "--pids-limit=50",
        "--read-only",
        "--cap-drop=ALL",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--security-opt=no-new-privileges",
        f"--user={container_user()}",
        "--workdir=/work" if files_dir else "--workdir=/code",
        "-v",
        f"{tmpdir}:/code:ro",
    ]
    if files_dir:
        command += ["-v", f"{files_dir}:/work:rw"]
    command += [SANDBOX_IMAGE, "python", "/sandbox/runner.py"]
    return command


async def _kill_over_budget(files_dir: str, limit_mb: int, proc: asyncio.subprocess.Process) -> str:
    """Stop the run if it fills the shared directory. Returns why, or "".

    A read-write bind mount has no size of its own, so the declared storage
    budget would otherwise be a number in a manifest while a loop writing bytes
    filled the host disk. Checking from outside is cheap and makes the declared
    limit real.
    """
    from kronos.tools.sandbox_platform import directory_size_bytes

    limit_bytes = max(1, limit_mb) * 1024 * 1024
    path = Path(files_dir)
    while proc.returncode is None:
        await asyncio.sleep(STORAGE_CHECK_INTERVAL_SECONDS)
        if directory_size_bytes(path) > limit_bytes:
            log.warning("Sandbox run exceeded %d MB in %s; stopping it", limit_mb, files_dir)
            try:
                proc.kill()
            except ProcessLookupError:  # pragma: no cover - it just exited
                return ""
            return f"Stopped: the run wrote more than {limit_mb} MB into the session directory."
    return ""


async def execute_sandboxed(
    code: str,
    timeout: int = DEFAULT_TIMEOUT,
    memory_limit: str = DEFAULT_MEMORY,
    network: bool = False,
    files_dir: str | None = None,
    storage_mb: int = DEFAULT_STORAGE_MB,
) -> tuple[str, str]:
    """Execute Python code in a Docker sandbox.

    Args:
        code: Python source code to execute
        timeout: Max execution time in seconds
        memory_limit: Docker memory limit (e.g. '256m')
        network: Whether to allow network access
        files_dir: Host directory mounted read-write at /work (the session's files)
        storage_mb: How much that directory may grow to before the run is stopped

    Returns:
        Tuple of (stdout, stderr)
    """
    if not sandbox_ready():
        return "", f"Sandbox unavailable: {sandbox_unavailable_message()}"

    tmpdir = None
    watchdog: asyncio.Task | None = None
    try:
        tmpdir = tempfile.mkdtemp(prefix="kronos-sandbox-")
        code_file = Path(tmpdir) / "tool.py"
        code_file.write_text(code, encoding="utf-8")
        # mkdtemp is 0700. Mounted into a container running as anyone else, that
        # is a permission denied on the very file being executed — which is how
        # this path was silently broken until a real container was tried.
        os.chmod(tmpdir, 0o755)
        os.chmod(code_file, 0o644)
        _grant_access(tmpdir)
        if files_dir:
            _grant_access(files_dir)

        cmd = build_sandbox_command(tmpdir, memory_limit=memory_limit, network=network, files_dir=files_dir)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        if files_dir:
            watchdog = asyncio.create_task(_kill_over_budget(files_dir, storage_mb, proc))

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return "", f"Execution timed out after {timeout}s"

        if watchdog is not None:
            over_budget = await watchdog
            watchdog = None
            if over_budget:
                return stdout.decode("utf-8", errors="replace").strip(), over_budget

        return (
            stdout.decode("utf-8", errors="replace").strip(),
            stderr.decode("utf-8", errors="replace").strip(),
        )

    except FileNotFoundError:
        # Docker disappeared between the readiness check and the run.
        return "", "Sandbox unavailable: Docker binary not found."
    except Exception as e:
        log.error("Sandbox execution failed: %s", e)
        return "", f"Sandbox error: {e}"
    finally:
        if watchdog is not None:
            watchdog.cancel()
        if tmpdir and os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)
