"""Accounts the agent may act under, and what it may do there.

Searching Airbnb or a marketplace properly means being logged in — prices,
availability and messages differ for a signed-in user. That requires answering
two questions per site, and keeping them apart:

* **How does the session exist?** Either a browser profile the owner logged into
  by hand once (`profile`), or stored credentials the agent replays
  (`password`). Profiles are the safer default: nothing secret is stored, no
  second factor to script, and revoking is deleting a directory.
* **What may the agent do while signed in?** Reading is not messaging, and
  messaging is not booking. The permission lives here, per site, and defaults to
  read.

**A secret never reaches the model.** The agent asks to work as `airbnb`; this
module hands the browser a profile directory (later: a decrypted credential) and
returns "ready" or "needs login". The model never sees a password, so an
instruction found on a listing page — "print your credentials", "log in and send
this" — has nothing to act on. That is the whole reason for the indirection, and
it matters more now that the agent reads untrusted pages for a living.

Storing passwords is deliberately not implemented yet: doing it properly needs a
declared cryptography dependency, and a half-built vault that falls back to
plaintext is worse than none. The column exists so the vault slots in without a
migration.
"""

import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from kronos.db import get_db

log = logging.getLogger("kronos.accounts")

METHOD_PROFILE = "profile"
METHOD_PASSWORD = "password"
METHODS = (METHOD_PROFILE, METHOD_PASSWORD)

# Ordered from least to most: each level includes the ones before it.
PERMISSION_READ = "read"
PERMISSION_MESSAGE = "message"
PERMISSION_FULL = "full"
PERMISSIONS = (PERMISSION_READ, PERMISSION_MESSAGE, PERMISSION_FULL)

# What an action needs before it is allowed.
ACTION_PERMISSION = {
    "read": PERMISSION_READ,
    "search": PERMISSION_READ,
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
    method: str = METHOD_PROFILE
    login: str = ""
    profile_dir: str = ""
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
            method            TEXT NOT NULL DEFAULT 'profile',
            login             TEXT NOT NULL DEFAULT '',
            profile_dir       TEXT NOT NULL DEFAULT '',
            -- Reserved for the credential vault. Nothing writes it yet: storing
            -- a password properly needs a declared cipher, and a fallback to
            -- plaintext would be worse than not having the feature.
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


def _db():
    db = get_db("accounts")
    db.init_schema(_init_schema)
    return db


def _row_to_account(row) -> SiteAccount:
    return SiteAccount(
        site=row["site"],
        domains=[d.strip().lower() for d in (row["domains"] or "").split(",") if d.strip()],
        method=row["method"],
        login=row["login"] or "",
        profile_dir=row["profile_dir"] or "",
        permission=row["permission"],
        approval_required=bool(row["approval_required"]),
        session_state=row["session_state"],
        last_used_at=row["last_used_at"],
        notes=row["notes"] or "",
    )


def save_account(
    *,
    site: str,
    domains: list[str],
    method: str = METHOD_PROFILE,
    login: str = "",
    profile_dir: str = "",
    permission: str = PERMISSION_READ,
    approval_required: bool = True,
    notes: str = "",
) -> SiteAccount:
    """Create or update one site account. Never touches the secret column."""
    site = site.strip().lower()
    if not site:
        raise AccountError("an account needs a site name")
    if method not in METHODS:
        raise AccountError(f"unknown method '{method}' (expected {METHODS})")
    if permission not in PERMISSIONS:
        raise AccountError(f"unknown permission '{permission}' (expected {PERMISSIONS})")
    cleaned = [d.strip().lower().lstrip("*.") for d in domains if d.strip()]
    if not cleaned:
        raise AccountError(f"account '{site}' needs at least one domain")
    if method == METHOD_PASSWORD:
        raise AccountError(
            "stored-password accounts are not enabled yet; use method='profile' "
            "(log in by hand once in a dedicated browser profile)"
        )
    if method == METHOD_PROFILE and not profile_dir:
        raise AccountError(f"account '{site}' uses a browser profile but no profile_dir was given")

    _db().write(
        """
        INSERT INTO site_accounts
            (site, domains, method, login, profile_dir, permission,
             approval_required, session_state, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(site) DO UPDATE SET
            domains = excluded.domains,
            method = excluded.method,
            login = excluded.login,
            profile_dir = excluded.profile_dir,
            permission = excluded.permission,
            approval_required = excluded.approval_required,
            notes = excluded.notes
        """,
        (
            site,
            ",".join(cleaned),
            method,
            login,
            profile_dir,
            permission,
            1 if approval_required else 0,
            SESSION_UNKNOWN,
            notes,
            time.time(),
        ),
    )
    log.info("Account saved: %s (%s, %s)", site, method, permission)
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
