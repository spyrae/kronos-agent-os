"""Running a short program instead of doing arithmetic in prose.

Forty listings, twenty product cards, a bank export in a format nobody planned
for — the work is normalising and counting, and a model doing that in its head is
where invented totals come from. This runs the code instead, in the same
container as everything else here: no network, read-only root, every capability
dropped, non-root, killed on time or on disk.

Files live per session and outlive the run. A step that writes ``offers.csv``
today and a plan step that reads it on Thursday are the same session, so the
session name defaults to the thread — which for a plan step is ``plan:<id>``,
meaning a plan accumulates its own working files without anyone naming them.

**No import blocklist.** The container is the boundary. A regex that rejects
``import os`` inside a network-less, capability-less container would refuse
legitimate code and buy nothing — and pretending otherwise is the false
confidence this module's neighbours were just cleaned of.
"""

import json
import logging
from pathlib import Path

from langchain_core.tools import tool

from kronos.audit import get_tool_audit_context
from kronos.config import settings
from kronos.security.effects import mark_side_effect

log = logging.getLogger("kronos.tools.code")

MAX_CODE_CHARS = 20_000
MAX_OUTPUT_CHARS = 8_000
MAX_INPUT_FILE_CHARS = 200_000
MAX_INPUT_FILES = 20
MAX_TIMEOUT_SECONDS = 120
DEFAULT_TIMEOUT_SECONDS = 30
LISTED_FILES = 25


def safe_filename(name: str) -> str:
    """A file name that cannot leave the session directory. Empty if hopeless."""
    cleaned = Path(str(name)).name.strip()
    if cleaned in ("", ".", ".."):
        return ""
    return "".join(char for char in cleaned if char.isalnum() or char in "._- ")[:80].strip()


def default_session() -> str:
    """The thread this call belongs to, so a plan's runs share their files."""
    context = get_tool_audit_context()
    return context.get("thread_id") or context.get("session_id") or "default"


def _write_inputs(files_dir: Path, raw: str) -> tuple[list[str], str]:
    """(names written, error). Text only: this is for data the agent already has."""
    if not raw.strip():
        return [], ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        return [], f"files must be a JSON object of name → text content: {e}"
    if not isinstance(payload, dict):
        return [], "files must be a JSON object of name → text content"
    if len(payload) > MAX_INPUT_FILES:
        return [], f"too many files ({len(payload)}; max {MAX_INPUT_FILES})"

    written = []
    for name, content in payload.items():
        safe = safe_filename(name)
        if not safe:
            return [], f"{name!r} is not a usable file name"
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        if len(text) > MAX_INPUT_FILE_CHARS:
            return [], f"{safe} is larger than {MAX_INPUT_FILE_CHARS} characters"
        (files_dir / safe).write_text(text, encoding="utf-8")
        written.append(safe)
    return written, ""


def _list_files(files_dir: Path) -> list[str]:
    entries = []
    for path in sorted(files_dir.rglob("*")):
        if path.is_file():
            entries.append(f"{path.relative_to(files_dir)} ({path.stat().st_size} bytes)")
    return entries


def _truncate(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n… truncated at {MAX_OUTPUT_CHARS} characters"


@tool
async def run_code(code: str, session: str = "", files: str = "", timeout: int = DEFAULT_TIMEOUT_SECONDS) -> str:
    """Run a short Python program in a locked-down container and return its output.

    Use it for anything countable: totalling costs, normalising scraped rows into
    one shape, parsing an odd export, checking dates. Print what you want back —
    stdout is the answer.

    The standard library only, no network. Files you write in the current
    directory stay for later calls with the same session, so one run can prepare
    data and the next can use it.

    Args:
        code: Python source. Print the result; nothing else is returned.
        session: Name for the working directory. Defaults to this conversation,
            so a plan's steps share their files.
        files: Optional JSON object of name → text to place next to the code
            before it runs, e.g. {"offers.json": "[{...}]"}.
        timeout: Seconds to allow (max 120).
    """
    if not settings.enable_code_execution:
        return (
            "[ERROR] Code execution is off. It is opt-in: set ENABLE_CODE_EXECUTION=true "
            "(or capabilities.code_execution in policy.yaml) and build the sandbox image."
        )
    if not code.strip():
        return "[ERROR] No code given."
    if len(code) > MAX_CODE_CHARS:
        return f"[ERROR] Code is longer than {MAX_CODE_CHARS} characters."
    try:
        compile(code, "<run_code>", "exec")
    except SyntaxError as e:
        return f"[ERROR] The code does not parse: {e}"

    from kronos.tools.sandbox import DEFAULT_MEMORY, execute_sandboxed, sandbox_ready, sandbox_unavailable_message
    from kronos.tools.sandbox_platform import (
        PolicyDecision,
        SandboxRunRequest,
        create_session_workspace,
        evaluate_policy,
        record_sandbox_decision,
        sandbox_policy,
    )

    limits = sandbox_policy().max_resources
    seconds = max(1, min(int(timeout), MAX_TIMEOUT_SECONDS, limits.timeout_seconds))
    request = SandboxRunRequest(
        tool_name="run_code",
        session_id=session.strip() or default_session(),
        # Named for the audit trail, not as a security claim: what actually
        # bounds this run is the container, the clock and the disk watchdog.
        input_mounts=("files",),
        output_mounts=("files",),
        resources=limits,
    )

    policy = sandbox_policy()
    decision = evaluate_policy(request, policy)
    if not decision.allowed:
        record_sandbox_decision(request, decision, policy)
        return f"[ERROR] Blocked by sandbox policy: {', '.join(decision.violations)}"

    if not sandbox_ready():
        record_sandbox_decision(
            request,
            PolicyDecision(False, "sandbox unavailable", ("execution:docker_not_ready",)),
            policy,
        )
        return f"[ERROR] {sandbox_unavailable_message()}"

    workspace = create_session_workspace(request)
    files_dir = Path(workspace["files"])
    written, error = _write_inputs(files_dir, files)
    if error:
        return f"[ERROR] {error}"

    stdout, stderr = await execute_sandboxed(
        code,
        timeout=seconds,
        memory_limit=DEFAULT_MEMORY,
        network=False,
        files_dir=str(files_dir),
        storage_mb=limits.storage_mb,
    )
    record_sandbox_decision(
        request,
        decision,
        policy,
        stdout=stdout,
        stderr=stderr,
        resources_used={"timeout_seconds": seconds, "storage_mb": limits.storage_mb},
    )

    return _render(request.session_id, written, stdout, stderr, _list_files(files_dir))


def _render(session: str, written: list[str], stdout: str, stderr: str, present: list[str]) -> str:
    parts = [f"Ran in session '{session}'."]
    if written:
        parts.append(f"Placed: {', '.join(written)}")
    if stdout:
        parts.append("Output:\n" + _truncate(stdout))
    if stderr:
        # Not folded into the output: code that printed a total and then failed
        # is not code that produced a total.
        parts.append("Errors:\n" + _truncate(stderr))
    if not stdout and not stderr:
        parts.append("The program printed nothing. stdout is the only thing returned — print the result.")
    if present:
        shown = present[:LISTED_FILES]
        more = f" (+{len(present) - len(shown)} more)" if len(present) > len(shown) else ""
        parts.append(f"Files in the session now: {', '.join(shown)}{more}")
    return "\n\n".join(parts)


# Running code writes files that outlive the turn, so durable resume must not
# replay it blindly — the second run would see the first run's leftovers.
CODE_TOOLS = mark_side_effect([run_code], reason="code execution")
