"""Repositories the agent may read, and the boundary around them.

Answering "why did the build break" or "what changed this week" from a phone
means the agent has to see the code. That is a different kind of access from
everything else it has: a repository is a directory tree on the machine the
agent runs on, and a directory tree has no edges unless someone draws them.

So three lines are drawn here, and each one exists because of a specific way
this goes wrong:

* **Only registered roots.** A path that is not inside a repository the owner
  added is refused. Without this, "read a file" is "read any file on the host".
* **Never outside the root.** Every path is resolved — symlinks included —
  before it is compared, because ``docs/../../../../etc/passwd`` and a symlink
  called ``notes`` pointing at ``/`` are the same attack written twice.
* **Secrets are not source.** A repository contains `.env`, keys, tokens and a
  `data/` directory nobody meant to publish. Reading those into a model's
  context is a leak that no later redaction undoes, so they are refused by name
  and by gitignore before anything is read.

Read-only on purpose. Committing and pushing need credentials and a decision
about which repositories may be changed unattended; until that exists, a
permission other than ``read`` is refused rather than quietly accepted.
"""

import fnmatch
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from kronos.db import get_db

log = logging.getLogger("kronos.repos")

PERMISSION_READ = "read"
PERMISSIONS = (PERMISSION_READ,)

# Names that are secrets wherever they appear. Matched on the file name and on
# every path segment, so `config/.env.production` and `keys/id_rsa` both fail.
SECRET_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p8",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "*.keystore",
    "*.jks",
    "credentials.json",
    "service-account*.json",
    "*.session",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "secrets.*",
    "*.sqlite",
    "*.db",
)

# Directories never worth reading and expensive to walk.
SKIP_DIRS = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build", ".next"}
)

MAX_FILE_BYTES = 400_000


class RepoError(Exception):
    """Raised when a repository is unknown, or a path is outside the lines."""


@dataclass
class Repo:
    name: str
    path: str
    permission: str = PERMISSION_READ
    notes: str = ""
    added_at: float = 0.0

    @property
    def root(self) -> Path:
        return Path(self.path)


def _init_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS repos (
            name        TEXT PRIMARY KEY,
            path        TEXT NOT NULL,
            permission  TEXT NOT NULL DEFAULT 'read',
            notes       TEXT NOT NULL DEFAULT '',
            added_at    REAL NOT NULL
        );
        """
    )


def _db():
    db = get_db("repos")
    db.init_schema(_init_schema)
    return db


def _row(row) -> Repo:
    return Repo(
        name=row["name"],
        path=row["path"],
        permission=row["permission"],
        notes=row["notes"] or "",
        added_at=row["added_at"],
    )


# --- the registry -------------------------------------------------------------


def add_repo(name: str, path: str, *, permission: str = PERMISSION_READ, notes: str = "") -> Repo:
    """Register a repository. The path must exist and be a directory."""
    name = name.strip().lower()
    if not name:
        raise RepoError("a repository needs a name")
    if permission not in PERMISSIONS:
        raise RepoError(
            f"permission '{permission}' is not available: this reads repositories and does not change them yet"
        )

    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise RepoError(f"{root} is not a directory")

    _db().write(
        """
        INSERT INTO repos (name, path, permission, notes, added_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET path = excluded.path, permission = excluded.permission, notes = excluded.notes
        """,
        (name, str(root), permission, notes, time.time()),
    )
    log.info("Repository registered: %s → %s", name, root)
    return get_repo(name)


def get_repo(name: str) -> Repo:
    row = _db().read_one("SELECT * FROM repos WHERE name = ?", (name.strip().lower(),))
    if row is None:
        known = ", ".join(repo.name for repo in list_repos()) or "none registered"
        raise RepoError(f"no repository called '{name}' (known: {known})")
    return _row(row)


def list_repos() -> list[Repo]:
    return [_row(row) for row in _db().read("SELECT * FROM repos ORDER BY name")]


def remove_repo(name: str) -> bool:
    return _db().write("DELETE FROM repos WHERE name = ?", (name.strip().lower(),)).rowcount > 0


# --- the boundary -------------------------------------------------------------


def looks_secret(relative: str) -> bool:
    """Whether a path names something that is a credential rather than source.

    Checked on every segment: a secret one directory down is still a secret, and
    `config/.env.production` is the shape this exists for.
    """
    for segment in Path(relative).parts:
        for pattern in SECRET_PATTERNS:
            if fnmatch.fnmatch(segment.lower(), pattern):
                return True
    return False


def _gitignore_patterns(root: Path) -> list[str]:
    """The repository's own idea of what does not belong in it.

    Not a security boundary — it is a strong hint. What a project gitignores is
    usually its local configuration, its data and its keys, which is exactly the
    set nobody wants read into a model's context.
    """
    patterns: list[str] = []
    ignore_file = root / ".gitignore"
    if not ignore_file.is_file():
        return patterns
    try:
        for line in ignore_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("!"):
                patterns.append(line.rstrip("/"))
    except OSError as e:  # pragma: no cover - unreadable .gitignore
        log.debug("Cannot read %s: %s", ignore_file, e)
    return patterns


def is_ignored(root: Path, relative: str) -> bool:
    patterns = _gitignore_patterns(root)
    parts = Path(relative).parts
    for pattern in patterns:
        if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(relative, f"{pattern}/*"):
            return True
        if any(fnmatch.fnmatch(part, pattern) for part in parts):
            return True
    return False


def resolve(repo: Repo, relative: str = "") -> Path:
    """Turn a path inside a repository into a real one, or refuse.

    The comparison happens after resolution, so a symlink pointing out of the
    tree is caught as well as ``../``. Both are the same request in different
    clothes.
    """
    root = repo.root.resolve()
    candidate = (root / relative).resolve() if relative else root

    if candidate != root and root not in candidate.parents:
        raise RepoError(f"{relative} is outside repository '{repo.name}'")

    inside = "" if candidate == root else str(candidate.relative_to(root))
    if inside and looks_secret(inside):
        raise RepoError(f"{inside} looks like a credential, not source — refusing to read it")
    if inside and is_ignored(root, inside):
        raise RepoError(f"{inside} is gitignored in '{repo.name}' — it is local state, not code")
    return candidate


def readable_file(repo: Repo, relative: str) -> Path:
    """A resolved path that is a file small enough to read."""
    target = resolve(repo, relative)
    if not target.is_file():
        raise RepoError(f"{relative} is not a file in '{repo.name}'")
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise RepoError(f"{relative} is {size:,} bytes — larger than this reads ({MAX_FILE_BYTES:,})")
    return target


def walk(repo: Repo, relative: str = "", *, limit: int = 500) -> list[str]:
    """Paths inside a repository worth showing, skipping noise and secrets."""
    start = resolve(repo, relative)
    root = repo.root.resolve()
    found: list[str] = []

    for path in sorted(start.rglob("*")):
        if len(found) >= limit:
            break
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if not path.is_file():
            continue
        inside = str(path.relative_to(root))
        if looks_secret(inside) or is_ignored(root, inside):
            continue
        found.append(inside)
    return found
