"""Reading a repository from wherever the agent happens to be.

"Why did the build break", "what changed this week", "show me that function" —
questions with a definite answer, asked from a phone. The answers come from the
working copy on the machine the agent runs on, through :mod:`kronos.repos`,
which is where the boundary lives.

Everything here is read-only, and everything it returns is **untrusted**: a
README, a comment or a commit message is text somebody else wrote, and a
repository is a perfectly ordinary place to hide an instruction aimed at an
agent. The same framing that covers fetched web pages covers this.

Git is read through the command line rather than a library — `log`, `diff`,
`status` and nothing else, with the repository path fixed by the registry, so
there is no argument through which another command could be reached.
"""

import asyncio
import logging

from langchain_core.tools import tool

from kronos import repos
from kronos.audit import redact_secrets
from kronos.security.untrusted import mark_untrusted

log = logging.getLogger("kronos.tools.repo")

GIT_TIMEOUT_SECONDS = 20
MAX_MATCHES = 40
MAX_MATCH_CHARS = 200
MAX_LINES = 400
MAX_DIFF_CHARS = 6000
DEFAULT_HISTORY = 10


async def _git(repo: repos.Repo, *args: str, ok_codes: tuple[int, ...] = (0,)) -> str:
    """Run one read-only git command inside a registered repository.

    ``ok_codes`` is how a caller says which exit codes are answers rather than
    failures: `git grep` returns 1 when nothing matched, and "nothing matched"
    is a result the owner asked for.
    """
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repo.root),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=GIT_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise repos.RepoError(f"git took longer than {GIT_TIMEOUT_SECONDS}s") from None

    if process.returncode not in ok_codes:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise repos.RepoError(detail or f"git exited {process.returncode}")
    return stdout.decode("utf-8", errors="replace")


@tool
async def repo_list() -> str:
    """List the repositories this agent can read, and where they are."""
    registered = repos.list_repos()
    if not registered:
        return "No repositories registered. Add one with `kaos repos add <name> <path>`."
    return "\n".join(f"- {repo.name}: {repo.path}" + (f" — {repo.notes}" if repo.notes else "") for repo in registered)


@tool
async def repo_tree(repo: str, path: str = "", limit: int = 200) -> str:
    """List the files in a repository, or in one directory of it.

    Build, dependency and cache directories are skipped, and so is anything the
    repository gitignores or that looks like a credential.

    Args:
        repo: Registered repository name — see repo_list.
        path: Optional subdirectory.
        limit: How many paths to return at most.
    """
    try:
        target = repos.get_repo(repo)
        found = repos.walk(target, path, limit=max(1, min(limit, 500)))
    except repos.RepoError as e:
        return f"[ERROR] {e}"

    if not found:
        return f"Nothing readable under {path or '/'} in '{repo}'."
    return f"{len(found)} file(s) in '{repo}':\n" + "\n".join(found)


@tool
async def repo_read(repo: str, path: str, start: int = 1, end: int = 0) -> str:
    """Read a file from a repository, optionally a range of lines.

    Args:
        repo: Registered repository name.
        path: File path inside the repository.
        start: First line (1-based).
        end: Last line; 0 means "as far as the limit allows".
    """
    try:
        target = repos.readable_file(repos.get_repo(repo), path)
        text = target.read_text(encoding="utf-8", errors="replace")
    except repos.RepoError as e:
        return f"[ERROR] {e}"
    except OSError as e:
        return f"[ERROR] Cannot read {path}: {e}"

    lines = text.splitlines()
    first = max(1, start)
    last = min(len(lines), end if end > 0 else first + MAX_LINES - 1)
    if first > len(lines):
        return f"[ERROR] {path} has {len(lines)} lines; {first} is past the end."

    body = "\n".join(f"{number:>5}  {line}" for number, line in enumerate(lines[first - 1 : last], start=first))
    header = f"{repo}/{path} lines {first}-{last} of {len(lines)}"
    truncated = "\n… more lines follow; ask for them by range." if last < len(lines) else ""
    return f"{header}\n{redact_secrets(body)}{truncated}"


@tool
async def repo_search(repo: str, pattern: str, path: str = "", limit: int = MAX_MATCHES) -> str:
    """Search a repository for a pattern and return the matching lines.

    Args:
        repo: Registered repository name.
        pattern: What to look for — a regular expression.
        path: Optional subdirectory to search in.
        limit: How many matches to return at most.
    """
    try:
        target = repos.get_repo(repo)
        # Resolved through the registry so the search cannot start outside it.
        repos.resolve(target, path)
        args = ["grep", "-n", "-I", "--untracked", "-e", pattern]
        if path:
            args += ["--", path]
        output = await _git(target, *args, ok_codes=(0, 1))
    except repos.RepoError as e:
        return f"[ERROR] {e}"

    matches = []
    for line in output.splitlines():
        if len(matches) >= max(1, min(limit, MAX_MATCHES)):
            break
        if repos.looks_secret(line.split(":", 1)[0]):
            continue  # never surface a line from a file that should not be read
        matches.append(line[:MAX_MATCH_CHARS])

    if not matches:
        return f"No match for /{pattern}/ in '{repo}'."
    more = "\n… more matches; narrow the pattern or the path." if len(output.splitlines()) > len(matches) else ""
    return f"{len(matches)} match(es) in '{repo}':\n" + redact_secrets("\n".join(matches)) + more


@tool
async def repo_history(repo: str, path: str = "", limit: int = DEFAULT_HISTORY) -> str:
    """Recent commits in a repository, or touching one path.

    Args:
        repo: Registered repository name.
        path: Optional file or directory to follow.
        limit: How many commits.
    """
    try:
        target = repos.get_repo(repo)
        if path:
            repos.resolve(target, path)
        args = ["log", f"-{max(1, min(limit, 50))}", "--date=short", "--pretty=%h %ad %an: %s"]
        if path:
            args += ["--", path]
        output = await _git(target, *args)
    except repos.RepoError as e:
        return f"[ERROR] {e}"

    return output.strip() or f"No commits in '{repo}'" + (f" touching {path}" if path else "") + "."


@tool
async def repo_diff(repo: str, ref: str = "", path: str = "") -> str:
    """What changed — uncommitted work by default, or against a ref.

    Args:
        repo: Registered repository name.
        ref: Compare against this ref (e.g. "main", "HEAD~3"). Empty = the
            working tree's own uncommitted changes.
        path: Optional file or directory to limit the diff to.
    """
    try:
        target = repos.get_repo(repo)
        if path:
            repos.resolve(target, path)
        args = ["diff", "--stat", "-p"]
        if ref:
            args.append(ref)
        if path:
            args += ["--", path]
        output = await _git(target, *args)
    except repos.RepoError as e:
        return f"[ERROR] {e}"

    if not output.strip():
        return f"Nothing changed in '{repo}'" + (f" against {ref}" if ref else "") + "."
    body = redact_secrets(output)
    if len(body) > MAX_DIFF_CHARS:
        body = body[:MAX_DIFF_CHARS] + "\n… diff truncated; narrow it with a path."
    return body


# A repository is text other people wrote. A comment, a README or a commit
# message is as good a place to hide an instruction as a web page, so the same
# untrusted framing applies.
REPO_TOOLS = mark_untrusted(
    [repo_list, repo_tree, repo_read, repo_search, repo_history, repo_diff],
    reason="repository contents",
)
