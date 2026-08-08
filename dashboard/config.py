"""Dashboard configuration.

The password is never written to the log. A generated one used to be printed
at startup, which put a live credential into the journal — readable by every
member of ``adm``/``systemd-journal``, and copied into whatever ships logs off
the host. It goes into a 0600 file instead, and only the path is logged: the
owner can still read it, a log reader cannot.

Storing it also makes it stable across restarts. The old value was rolled on
every start, which is precisely why printing it felt necessary.
"""

import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger("kronos.dashboard")

DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8789"))
DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")


def _password_file() -> Path:
    """Where a generated password is kept — per-agent, alongside its databases."""
    override = os.environ.get("DASHBOARD_PASSWORD_FILE", "").strip()
    if override:
        return Path(override)

    from kronos.config import settings

    return Path(settings.db_dir) / "dashboard_password"


def _read_or_create_password(path: Path) -> str:
    """Return the stored password, creating a 0600 file on first run.

    Returns an empty string when the secret cannot be persisted — the caller
    treats that as "no dashboard", which is safer than serving one whose
    password only exists in memory.
    """
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.error("Cannot read dashboard password file %s: %s", path, exc)
        return ""

    password = secrets.token_urlsafe(24)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create with the restrictive mode already in place, so the secret is
        # never briefly world-readable between creation and chmod.
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        path.write_text(password + "\n", encoding="utf-8")
    except OSError as exc:
        log.error("Cannot write dashboard password file %s: %s", path, exc)
        return ""
    return password


DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "").strip()
DASHBOARD_PASSWORD_GENERATED = False
DASHBOARD_PASSWORD_PATH: Path | None = None

if not DASHBOARD_PASSWORD:
    DASHBOARD_PASSWORD_PATH = _password_file()
    DASHBOARD_PASSWORD = _read_or_create_password(DASHBOARD_PASSWORD_PATH)
    DASHBOARD_PASSWORD_GENERATED = bool(DASHBOARD_PASSWORD)
