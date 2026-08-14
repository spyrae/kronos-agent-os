"""Accounts the agent may act under, and what it may do there.

Searching Airbnb or a marketplace properly means being logged in — prices,
availability and messages differ for a signed-in user. That requires answering
two questions per site, and keeping them apart:

* **How does the session exist?** Every account uses a browser profile the
  session lives in. Some also have a password in the vault, which lets the agent
  sign in again by itself when that session expires; without one, an expired
  session is a message to the owner. Whether a password is stored is not a
  separate setting — it is simply whether one is there.
* **What may the agent do while signed in?** Reading is not messaging, and
  messaging is not booking. The permission lives here, per site, and defaults to
  read.

**A secret never reaches the model.** The agent asks to work as `airbnb`; this
module hands the browser a profile directory, and — only when signing in — a
decrypted password that goes straight into the page. What comes back is "ready"
or "needs login". The model never sees a password, so an instruction found on a
listing page — "print your credentials", "log in and send this" — has nothing to
act on. That is the whole reason for the indirection, and it matters more now
that the agent reads untrusted pages for a living.

Passwords are encrypted by :mod:`kronos.vault` and never returned by any listing
or read path. The single function that produces plaintext is
:func:`use_password`, and it records every call in the tamper-evident audit log —
without the value.
"""

import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from kronos import audit, vault
from kronos.config import settings
from kronos.db import get_db

log = logging.getLogger("kronos.accounts")

# Ordered from least to most: each level includes the ones before it.
PERMISSION_READ = "read"
PERMISSION_MESSAGE = "message"
PERMISSION_FULL = "full"
PERMISSIONS = (PERMISSION_READ, PERMISSION_MESSAGE, PERMISSION_FULL)

# What an action needs before it is allowed. Signing in sits at read level on
# purpose: a read-only account with a stored password exists precisely so it can
# keep its own session alive without asking.
ACTION_PERMISSION = {
    "read": PERMISSION_READ,
    "search": PERMISSION_READ,
    "login": PERMISSION_READ,
    "message": PERMISSION_MESSAGE,
    "reply": PERMISSION_MESSAGE,
    "book": PERMISSION_FULL,
    "order": PERMISSION_FULL,
    "pay": PERMISSION_FULL,
    "review": PERMISSION_FULL,
}

SESSION_UNKNOWN = "unknown"
SESSION_OK = "ok"
SESSION_EXPIRED = "expired"


class AccountError(Exception):
    """Raised when an account is missing, misconfigured, or not permitted."""


@dataclass
class SiteAccount:
    site: str
    domains: list[str] = field(default_factory=list)
    login: str = ""
    # Where the sign-in form lives, when the site does not simply show one on the
    # page the agent landed on. Must be inside `domains` — a login URL pointing
    # somewhere else is the exact shape of a phishing page.
    login_url: str = ""
    profile_dir: str = ""
    # Whether a password is in the vault. Never the password, and never a hint
    # about it — this is the only thing any read path says on the subject.
    has_password: bool = False
    permission: str = PERMISSION_READ
    approval_required: bool = True
    session_state: str = SESSION_UNKNOWN
    last_used_at: float | None = None
    notes: str = ""

    def allows(self, action: str) -> bool:
        """Whether this account's permission covers the action.

        An unknown action is refused rather than assumed harmless: the list of
        things one can do while signed in only ever grows, and the safe reading
        of "we have not classified this yet" is no.
        """
        needed = ACTION_PERMISSION.get(action.strip().lower())
        if needed is None:
            return False
        return PERMISSIONS.index(self.permission) >= PERMISSIONS.index(needed)

    def covers(self, url: str) -> bool:
        """Whether a URL belongs to this account's declared domains.

        Suffix match on host boundaries, so `booking.com` covers
        `secure.booking.com` but never `booking.com.evil.test` — a phished
        lookalike is exactly what a page-borne instruction would point at.
        """
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return False
        return any(host == domain or host.endswith(f".{domain}") for domain in self.domains)


def _init_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS site_accounts (
            site              TEXT PRIMARY KEY,
            domains           TEXT NOT NULL DEFAULT '',
            login             TEXT NOT NULL DEFAULT '',
            login_url         TEXT NOT NULL DEFAULT '',
            profile_dir       TEXT NOT NULL DEFAULT '',
            -- The password, encrypted by kronos.vault and bound to this row's
            -- site name. Read by exactly one function (use_password); no listing
            -- or view path selects it.
            secret            BLOB,
            permission        TEXT NOT NULL DEFAULT 'read',
            approval_required INTEGER NOT NULL DEFAULT 1,
            session_state     TEXT NOT NULL DEFAULT 'unknown',
            notes             TEXT NOT NULL DEFAULT '',
            last_used_at      REAL,
            created_at        REAL NOT NULL
        );
        """
    )
    # login_url arrived with automatic sign-in. Backfill on databases created
    # before it, the same way session.py does for its own late columns.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(site_accounts)").fetchall()}
    if "login_url" not in columns:
        conn.execute("ALTER TABLE site_accounts ADD COLUMN login_url TEXT NOT NULL DEFAULT ''")


def _db():
    db = get_db("accounts")
    db.init_schema(_init_schema)
    return db


def _row_to_account(row) -> SiteAccount:
    return SiteAccount(
        site=row["site"],
        domains=[d.strip().lower() for d in (row["domains"] or "").split(",") if d.strip()],
        login=row["login"] or "",
        login_url=row["login_url"] or "",
        profile_dir=row["profile_dir"] or "",
        has_password=bool(row["secret"]),
        permission=row["permission"],
        approval_required=bool(row["approval_required"]),
        session_state=row["session_state"],
        last_used_at=row["last_used_at"],
        notes=row["notes"] or "",
    )


def _default_profile_dir(site: str) -> str:
    """Where a site's browser session lives when the owner did not choose.

    Accounts with a stored password never need the owner to open the profile by
    hand, so making them invent a path would be ceremony.
    """
    return str(Path(settings.db_dir) / "browser-profiles" / site)


# What a Chromium user-data directory contains. Checked because the failure of a
# wrong directory is late and confusing: the browser starts on an empty profile,
# the site shows a login wall, and the account reports "session expired" forever
# without anyone suspecting the path.
PROFILE_MARKERS = ("Default", "Preferences", "Local State")


def looks_like_profile(path: Path) -> bool:
    return path.is_dir() and any((path / marker).exists() for marker in PROFILE_MARKERS)


def import_profile(site: str, source: str) -> SiteAccount:
    """Take a browser profile signed in elsewhere and make it this account's.

    The headless answer to "how do I log in on a machine with no screen": sign in
    on a laptop, copy the profile directory over, point the account at it. The
    copy lands in the agent's own data directory at 0700 — it holds live session
    cookies, which is to say credentials, and a profile readable by others is the
    same leak as a readable key file.
    """
    account = get_account(site)
    origin = Path(source).expanduser().resolve()
    if not origin.is_dir():
        raise AccountError(f"{origin} is not a directory")
    if not looks_like_profile(origin):
        raise AccountError(
            f"{origin} does not look like a browser profile (expected one of {', '.join(PROFILE_MARKERS)} inside it)"
        )

    target = Path(_default_profile_dir(account.site))
    if origin == target.resolve():
        raise AccountError(f"{origin} is already this account's profile directory")

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(origin, target, symlinks=False, ignore_dangling_symlinks=True)
    os.chmod(target, 0o700)

    _db().write("UPDATE site_accounts SET profile_dir = ? WHERE site = ?", (str(target), account.site))
    log.info("Imported a browser profile for %s", account.site)
    return get_account(site)


def _stored_secret(site: str) -> bytes | None:
    row = _db().read_one("SELECT secret FROM site_accounts WHERE site = ?", (site,))
    return row["secret"] if row else None


def save_account(
    *,
    site: str,
    domains: list[str],
    login: str = "",
    login_url: str = "",
    profile_dir: str = "",
    permission: str = PERMISSION_READ,
    approval_required: bool = True,
    notes: str = "",
    password: str = "",
) -> SiteAccount:
    """Create or update one site account, optionally storing its password.

    An empty ``password`` leaves whatever is already in the vault alone — saving
    an account to change its permission must not silently wipe its credentials.
    Use :func:`clear_password` to remove one.
    """
    site = site.strip().lower()
    if not site:
        raise AccountError("an account needs a site name")
    if permission not in PERMISSIONS:
        raise AccountError(f"unknown permission '{permission}' (expected {PERMISSIONS})")
    cleaned = [d.strip().lower().lstrip("*.") for d in domains if d.strip()]
    if not cleaned:
        raise AccountError(f"account '{site}' needs at least one domain")
    if password and not login:
        raise AccountError(f"account '{site}' has a password but no login to use it with")
    if login_url and not SiteAccount(site=site, domains=cleaned).covers(login_url):
        raise AccountError(
            f"login_url {login_url} is not one of the domains declared for '{site}' "
            f"({', '.join(cleaned)}) — that is the shape of a phishing page, not a login"
        )

    has_credential = bool(password) or _stored_secret(site) is not None
    if not profile_dir:
        if not has_credential:
            raise AccountError(
                f"account '{site}' needs either a profile_dir (log in by hand once in that "
                f"browser profile) or a stored password"
            )
        profile_dir = _default_profile_dir(site)

    _db().write(
        """
        INSERT INTO site_accounts
            (site, domains, login, login_url, profile_dir, permission,
             approval_required, session_state, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(site) DO UPDATE SET
            domains = excluded.domains,
            login = excluded.login,
            login_url = excluded.login_url,
            profile_dir = excluded.profile_dir,
            permission = excluded.permission,
            approval_required = excluded.approval_required,
            notes = excluded.notes
        """,
        (
            site,
            ",".join(cleaned),
            login,
            login_url,
            profile_dir,
            permission,
            1 if approval_required else 0,
            SESSION_UNKNOWN,
            notes,
            time.time(),
        ),
    )
    log.info("Account saved: %s (%s)", site, permission)
    if password:
        set_password(site, password)
    return get_account(site)


def get_account(site: str) -> SiteAccount:
    row = _db().read_one("SELECT * FROM site_accounts WHERE site = ?", (site.strip().lower(),))
    if row is None:
        known = ", ".join(a.site for a in list_accounts()) or "none configured"
        raise AccountError(f"no account for '{site}' (known: {known})")
    return _row_to_account(row)


def list_accounts() -> list[SiteAccount]:
    rows = _db().read("SELECT * FROM site_accounts ORDER BY site")
    return [_row_to_account(row) for row in rows]


def delete_account(site: str) -> bool:
    cursor = _db().write("DELETE FROM site_accounts WHERE site = ?", (site.strip().lower(),))
    return cursor.rowcount > 0


def set_password(site: str, password: str) -> None:
    """Put a site password in the vault. Nothing reads it back but use_password.

    Refuses when the vault has no key rather than storing anything readable —
    the whole feature is worth less than nothing if it can degrade to plaintext.
    """
    site = site.strip().lower()
    account = get_account(site)
    if not account.login:
        raise AccountError(f"account '{site}' has no login; a password on its own cannot sign in")
    if not password:
        raise AccountError("refusing to store an empty password (use clear_password to remove one)")

    try:
        blob = vault.encrypt(password, context=site)
    except vault.VaultError as e:
        raise AccountError(f"cannot store a password for '{site}': {e}") from e

    _db().write("UPDATE site_accounts SET secret = ? WHERE site = ?", (blob, site))
    audit.log_credential_event(site=site, event="stored", ok=True)
    log.info("Password stored for %s", site)


def clear_password(site: str) -> bool:
    """Forget a stored password. Returns whether there was one."""
    site = site.strip().lower()
    if _stored_secret(site) is None:
        return False
    _db().write("UPDATE site_accounts SET secret = NULL WHERE site = ?", (site,))
    audit.log_credential_event(site=site, event="cleared", ok=True)
    log.info("Password cleared for %s", site)
    return True


def use_password(site: str, *, purpose: str) -> str:
    """Decrypt a stored password. The only function in the codebase that can.

    Callers must hand the result straight to the browser and let it go: it must
    not be returned from a tool, put in a message, written to the session, or
    logged. Every call is recorded — without the value — so the owner can see
    when their credentials were used and for what.
    """
    account = get_account(site)
    blob = _stored_secret(account.site)
    if not blob:
        raise AccountError(f"no password stored for '{account.site}'")

    try:
        secret = vault.decrypt(blob, context=account.site)
    except vault.VaultError as e:
        audit.log_credential_event(site=account.site, event="used", ok=False, purpose=purpose, detail=str(e))
        raise AccountError(f"cannot use the stored password for '{account.site}': {e}") from e

    audit.log_credential_event(site=account.site, event="used", ok=True, purpose=purpose)
    return secret


def record_use(site: str, *, session_state: str = "") -> None:
    """Stamp an account as used, and optionally update what we learned."""
    if session_state and session_state not in (SESSION_UNKNOWN, SESSION_OK, SESSION_EXPIRED):
        raise AccountError(f"unknown session state '{session_state}'")
    if session_state:
        _db().write(
            "UPDATE site_accounts SET last_used_at = ?, session_state = ? WHERE site = ?",
            (time.time(), session_state, site.strip().lower()),
        )
    else:
        _db().write(
            "UPDATE site_accounts SET last_used_at = ? WHERE site = ?",
            (time.time(), site.strip().lower()),
        )


def authorise(site: str, action: str, *, url: str = "") -> SiteAccount:
    """The one gate: may this account do this, here? Raises if not.

    Returns the account so callers cannot accidentally use one they did not
    check — the permission and the handle come from the same call.
    """
    account = get_account(site)
    if not account.allows(action):
        raise AccountError(
            f"account '{site}' is set to '{account.permission}', which does not allow '{action}'. "
            f"Raise it in the dashboard if that is intended."
        )
    if url and not account.covers(url):
        raise AccountError(f"{url} is not one of the domains declared for '{site}' ({', '.join(account.domains)})")
    return account


def needs_approval(site: str, action: str) -> bool:
    """Whether this action should pause for a human even when permitted.

    Reading never does. Anything that leaves a trace on the account does, unless
    the owner turned that off for this site — because the agent doing something
    under someone's name is the part that cannot be undone by a retry.
    """
    if ACTION_PERMISSION.get(action.strip().lower()) == PERMISSION_READ:
        return False
    try:
        return get_account(site).approval_required
    except AccountError:
        return True
