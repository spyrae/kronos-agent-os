"""Docker sandbox for dynamic tool execution.

Runs untrusted code in isolated Docker containers instead of exec() in-process.
Falls back to in-process exec() if Docker is unavailable.
"""

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger("kronos.tools.sandbox")

SANDBOX_IMAGE = "kronos-sandbox:latest"
DEFAULT_TIMEOUT = 30
DEFAULT_MEMORY = "256m"


def _docker_available() -> bool:
    """Check if Docker is available."""
    return shutil.which("docker") is not None


async def execute_sandboxed(
    code: str,
    timeout: int = DEFAULT_TIMEOUT,
    memory_limit: str = DEFAULT_MEMORY,
    network: bool = False,
) -> tuple[str, str]:
    """Execute Python code in a Docker sandbox.

    Args:
        code: Python source code to execute
        timeout: Max execution time in seconds
        memory_limit: Docker memory limit (e.g. '256m')
        network: Whether to allow network access

    Returns:
        Tuple of (stdout, stderr)
    """
    if not _docker_available():
        log.warning("Docker not available, falling back to in-process exec")
        return _exec_in_process(code, timeout)

    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix="kronos-sandbox-")
        code_file = Path(tmpdir) / "tool.py"
        code_file.write_text(code, encoding="utf-8")

        network_flag = "bridge" if network else "none"

        cmd = [
            "docker", "run",
            "--rm",
            f"--memory={memory_limit}",
            f"--network={network_flag}",
            "--cpus=1",
            "--pids-limit=50",
            "--read-only",
            "--tmpfs=/tmp:size=64m",
            "--security-opt=no-new-privileges",
            "-v", f"{tmpdir}:/code:ro",
            SANDBOX_IMAGE,
            "python", "/code/tool.py",
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return "", f"Execution timed out after {timeout}s"

        return (
            stdout.decode("utf-8", errors="replace").strip(),
            stderr.decode("utf-8", errors="replace").strip(),
        )

    except FileNotFoundError:
        log.warning("Docker binary not found, falling back to in-process exec")
        return _exec_in_process(code, timeout)
    except Exception as e:
        log.error("Sandbox execution failed: %s", e)
        return "", f"Sandbox error: {e}"
    finally:
        if tmpdir and os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)


def _exec_in_process(code: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[str, str]:
    """Fallback: execute code in-process (unsafe, for dev/testing only)."""
    import io
    import sys

    log.warning("Executing dynamic tool in-process (no sandbox isolation)")

    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdout = captured_out = io.StringIO()
    sys.stderr = captured_err = io.StringIO()

    try:
        namespace: dict = {}
        exec(code, namespace)  # noqa: S102
        return captured_out.getvalue().strip(), captured_err.getvalue().strip()
    except Exception as e:
        return "", f"Execution error: {e}"
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr
