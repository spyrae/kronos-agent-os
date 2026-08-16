"""Docker sandbox for running code the agent wrote.

Everything here runs in a container with no network, a read-only root, every
capability dropped and a non-root uid. **There is no fallback.** An earlier
version dropped to ``exec()`` in the agent's own process when Docker was
missing and ``require_dynamic_tool_sandbox`` was off — a mode described in its
own log line as unsafe, one environment variable away from arbitrary code in
production. Missing Docker now means the code does not run.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from kronos.health import STATUS_BROKEN, STATUS_OFF, STATUS_OK, HealthCheck

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
        f"{Path(tmpdir).resolve()}:/code:ro",
    ]
    if files_dir:
        # Absolute, always. Docker reads a relative path as a *named volume*, so
        # `-v data/kronos/sandbox/x/files:/work` fails with "invalid characters
        # for a local volume name" — and only on a host whose data directory is
        # configured relatively, which is every real deployment and no test.
        command += ["-v", f"{Path(files_dir).resolve()}:/work:rw"]
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


# ── Health ────────────────────────────────────────────────────────────────────
#
# `sandbox_ready()` asks whether Docker and the image exist. Both of this
# module's real bugs passed that question and failed everything after it: a
# temp directory at 0700 mounted into a container running as another user, so
# every run died on `Permission denied: '/code/tool.py'`; and a bind mount
# passed relatively, which Docker reads as a *named volume* — broken on every
# host whose data directory is configured relatively, which is every real
# deployment and no test.
#
# A configuration check cannot see either. Only running code can, so that is
# what this does. And since a sandbox that runs but does not contain is worse
# than one that refuses to start, the same run checks that the walls are still
# up: no network, a read-only root, and not running as root.

PROBE_MARKER = "kronos-sandbox-probe"
PROBE_SESSION = "health-check"
PROBE_TIMEOUT = 60

CHECK_DOCKER = "docker"
CHECK_IMAGE = "image"
CHECK_EXECUTION = "execution"
CHECK_WORKSPACE = "workspace"
CHECK_NO_NETWORK = "no_network"
CHECK_READONLY_ROOT = "readonly_root"
CHECK_NON_ROOT = "non_root"

# The ones that are about safety rather than capability. A caller reporting a
# failure needs to tell the two apart: losing execution costs a feature, losing
# containment means code is still running with a wall down.
CONTAINMENT_CHECKS = frozenset({CHECK_NO_NETWORK, CHECK_READONLY_ROOT, CHECK_NON_ROOT})

# Reports uid, where it could route a packet, and whether its root filesystem
# took a write. Everything is answered from inside the container: asking the
# network question by dialling out would send the very packet it is checking for.
#
# The routing table, not the interface list, is what decides the network answer.
# Some kernels put phantom tunnel stubs (tunl0, sit0) into every new namespace,
# so `interfaces == ["lo"]` would report a breach on those hosts and be wrong —
# and a checker that cries wolf is one people learn to ignore. A stub carries no
# route; a bridge carries a default one. Measured empty under --network=none.
_PROBE_CODE = f"""
import json, os

report = {{"marker": "{PROBE_MARKER}", "uid": os.getuid()}}

try:
    with open("/proc/net/route") as handle:
        report["routes"] = [line.split()[0] for line in handle.read().splitlines()[1:] if line.strip()]
except OSError as e:
    report["routes"] = None
    report["routes_error"] = str(e)

try:
    report["interfaces"] = sorted(os.listdir("/sys/class/net"))
except OSError:
    report["interfaces"] = None

try:
    with open("/{PROBE_MARKER}", "w") as handle:
        handle.write("x")
    report["root_writable"] = True
except OSError:
    report["root_writable"] = False

print(json.dumps(report))
"""


def _probe_workspace() -> Path:
    """Where the workspace probe writes, derived exactly as a real run's is."""
    from kronos.tools.sandbox_platform import sandbox_workspace_root

    return sandbox_workspace_root() / PROBE_SESSION / "files"


async def check_sandbox_health() -> list[HealthCheck]:
    """Prove the sandbox can run code, and that it still contains what it runs.

    A check that could not be *determined* is left out rather than guessed at:
    when nothing can run, the containment guarantees are absent from the list
    instead of being reported as broken, and they come back as soon as
    execution does.
    """
    if not _docker_available():
        # Not a fault: a host without Docker is a host that opted out of running
        # code, and the tools that need it already refuse rather than fall back.
        return [HealthCheck(CHECK_DOCKER, STATUS_OFF, "docker is not installed — code execution is unavailable here")]

    checks = [HealthCheck(CHECK_DOCKER, STATUS_OK, "docker is available")]

    if not _docker_image_available():
        checks.append(HealthCheck(CHECK_IMAGE, STATUS_BROKEN, sandbox_unavailable_message()))
        return checks
    checks.append(HealthCheck(CHECK_IMAGE, STATUS_OK, SANDBOX_IMAGE))

    stdout, stderr = await execute_sandboxed(_PROBE_CODE, timeout=PROBE_TIMEOUT)
    report = _parse_probe(stdout)
    if report is None:
        checks.append(
            HealthCheck(
                CHECK_EXECUTION, STATUS_BROKEN, (stderr or stdout).strip()[:300] or "the container printed nothing"
            )
        )
        return checks

    checks.append(HealthCheck(CHECK_EXECUTION, STATUS_OK, "ran code and read its output back"))
    checks.extend(_containment_checks(report))
    checks.append(await _check_workspace())
    return checks


def _parse_probe(stdout: str) -> dict | None:
    """The probe's own JSON, or None when the container said something else."""
    for line in reversed(stdout.strip().splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("marker") == PROBE_MARKER:
            return parsed
    return None


def _containment_checks(report: dict) -> list[HealthCheck]:
    """Whether the walls the design promises are actually standing."""
    routes = report.get("routes")
    interfaces = report.get("interfaces") or []
    if routes is None:
        # Failing to read the routing table is not evidence that it is empty.
        network = HealthCheck(
            CHECK_NO_NETWORK,
            STATUS_BROKEN,
            f"could not read the container's routing table: {report.get('routes_error')}",
        )
    elif routes:
        network = HealthCheck(
            CHECK_NO_NETWORK,
            STATUS_BROKEN,
            f"the container can route through {', '.join(sorted(set(routes)))} — --network=none is not in force",
        )
    else:
        network = HealthCheck(
            CHECK_NO_NETWORK,
            STATUS_OK,
            f"no routes ({', '.join(interfaces) or 'no interfaces'})",
        )

    if report.get("root_writable"):
        readonly = HealthCheck(CHECK_READONLY_ROOT, STATUS_BROKEN, "the container wrote to its own root filesystem")
    else:
        readonly = HealthCheck(CHECK_READONLY_ROOT, STATUS_OK, "root filesystem refused a write")

    uid = report.get("uid")
    if uid == 0:
        non_root = HealthCheck(CHECK_NON_ROOT, STATUS_BROKEN, "the container is running as root")
    else:
        non_root = HealthCheck(CHECK_NON_ROOT, STATUS_OK, f"running as uid {uid}")

    return [network, readonly, non_root]


async def _check_workspace() -> HealthCheck:
    """A file written at /work must survive the container that wrote it.

    This is the mount that broke: passed relatively, Docker read it as a named
    volume and every run with session files failed. The probe derives the path
    the way a real run does rather than handing in an absolute one, so a
    regression in that derivation shows up here.
    """
    files_dir = _probe_workspace()
    marker = files_dir / f"{PROBE_MARKER}.txt"
    try:
        files_dir.mkdir(parents=True, exist_ok=True)
        marker.unlink(missing_ok=True)
    except OSError as e:
        return HealthCheck(CHECK_WORKSPACE, STATUS_BROKEN, f"cannot prepare the session directory: {e}")

    code = f"open({marker.name!r}, 'w').write('ok')\nprint('wrote')"
    stdout, stderr = await execute_sandboxed(code, timeout=PROBE_TIMEOUT, files_dir=str(files_dir))

    try:
        if not marker.exists():
            detail = (stderr or stdout or "the file was not there afterwards").strip()[:300]
            return HealthCheck(CHECK_WORKSPACE, STATUS_BROKEN, detail)
        return HealthCheck(CHECK_WORKSPACE, STATUS_OK, "a file written at /work survived the run")
    finally:
        marker.unlink(missing_ok=True)
