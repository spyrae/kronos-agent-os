"""Encrypting the few secrets this agent must hold — and nothing else.

The agent needs the owner's site passwords to sign in as them, and it must never
show one. Those are different problems: this module solves storage, and the
callers solve exposure by handing plaintext straight to the browser instead of
into a tool result.

**Fail closed, never plaintext.** With no key configured, storing a secret
raises. There is deliberately no "store it unencrypted for now" path: a vault
with a fallback is a plaintext store with extra steps, and the fallback is what
survives to production.

**What this protects against.** A copy of the database leaving the host — a
backup, an exported bundle, an rsync of one file, someone reading the SQLite in
a text editor. It does not protect against an attacker who already has the key
and the data together; nothing at this layer could. That is why the key can live
in the environment (`VAULT_KEY`) rather than on disk, and why the default key
file sits at 0600 and is reported by ``kaos vault status`` — so the owner can
see where the trust actually sits instead of assuming.

**Each secret is bound to what it belongs to.** ``context`` (the site name) is
authenticated but not encrypted: a blob moved from one row to another fails to
decrypt rather than quietly unlocking under a different account's permissions.
"""

import base64
import logging
import os
import secrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from kronos.config import settings

log = logging.getLogger("kronos.vault")

MAGIC = b"kaosv1"
KEY_BYTES = 32
NONCE_BYTES = 12

SOURCE_ENV = "env"
SOURCE_FILE = "file"
SOURCE_NONE = ""

# What to tell a human who hits a locked vault. One message, one place: the
# dashboard, the CLI and the tools all show this rather than inventing three
# different ways to say the same thing.
NO_KEY_HINT = "no vault key configured — run `kaos vault init`, or set VAULT_KEY in the environment"


class VaultError(Exception):
    """Raised when the vault has no usable key, or a secret will not open."""


def default_key_path() -> Path:
    """Where the key file lives unless configured otherwise.

    Beside the data it protects, which is honest about the limit: this defends a
    database that travels without its directory, not a host someone owns. Set
    ``VAULT_KEY_PATH`` outside ``data/``, or ``VAULT_KEY`` in the environment, if
    the deployment can keep them apart.
    """
    configured = (settings.vault_key_path or "").strip()
    return Path(configured) if configured else Path(settings.db_dir) / "vault.key"


def generate_key() -> str:
    """A fresh key, base64url-encoded for an env var or a key file."""
    return base64.urlsafe_b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")


def _decode_key(encoded: str, *, origin: str) -> bytes:
    """Decode and check a key, refusing anything that is not exactly right.

    A truncated env var must not silently become a short key: that is precisely
    the failure nobody notices until the ciphertext is worthless.
    """
    trimmed = encoded.strip()
    try:
        # validate=True on purpose: by default base64 discards characters outside
        # the alphabet, so a mistyped key would decode to a shorter, different
        # key instead of failing — the quietest possible way to lose a vault.
        raw = base64.b64decode(trimmed + "=" * (-len(trimmed) % 4), altchars=b"-_", validate=True)
    except Exception as e:
        raise VaultError(f"vault key from {origin} is not valid base64: {e}") from e
    if len(raw) != KEY_BYTES:
        raise VaultError(f"vault key from {origin} is {len(raw)} bytes, expected {KEY_BYTES} — was it truncated?")
    return raw


def _read_key_file(path: Path) -> str:
    if os.name == "posix":
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise VaultError(f"vault key file {path} is readable by others (mode {mode:o}); run: chmod 600 {path}")
    return path.read_text(encoding="utf-8").strip()


def key_source() -> str:
    """Where the key comes from: ``env``, ``file``, or empty when there is none.

    Never returns the key. The dashboard shows this so the owner can tell an
    environment-held key from one sitting next to the database.
    """
    if (settings.vault_key or "").strip():
        return SOURCE_ENV
    path = default_key_path()
    return SOURCE_FILE if path.is_file() else SOURCE_NONE


def available() -> bool:
    """Whether a usable key exists. A broken key counts as no key."""
    try:
        load_key()
    except VaultError:
        return False
    return True


def load_key() -> bytes:
    """The key, from the environment first, then the key file. Never logged.

    Read every time rather than cached: rotating a key should take effect on the
    next use, not the next restart, and a cached key is one more copy in memory.
    """
    configured = (settings.vault_key or "").strip()
    if configured:
        return _decode_key(configured, origin="VAULT_KEY")

    path = default_key_path()
    if not path.is_file():
        raise VaultError(NO_KEY_HINT)
    try:
        contents = _read_key_file(path)
    except VaultError:
        raise
    except OSError as e:
        raise VaultError(f"cannot read vault key file {path}: {e}") from e
    if not contents:
        raise VaultError(f"vault key file {path} is empty")
    return _decode_key(contents, origin=str(path))


def create_key_file(path: Path | None = None, *, overwrite: bool = False) -> Path:
    """Write a new key file at 0600. Refuses to overwrite unless asked.

    Overwriting is how every stored secret becomes unreadable at once, so it
    takes an explicit flag rather than a confirmation nobody reads.
    """
    target = path or default_key_path()
    if target.exists() and not overwrite:
        raise VaultError(f"{target} already exists — every stored secret is encrypted with it; refusing to replace it")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Create with the right mode from the start: a key that is briefly
    # world-readable has already been readable.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(generate_key() + "\n")
    if os.name == "posix":
        os.chmod(target, 0o600)  # in case the file already existed with a looser mode
    log.info("Vault key created: %s", target)
    return target


def encrypt(plaintext: str, *, context: str) -> bytes:
    """Encrypt one secret, bound to the context it belongs to."""
    if not plaintext:
        raise VaultError("refusing to store an empty secret")
    key = load_key()
    nonce = secrets.token_bytes(NONCE_BYTES)
    sealed = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), context.encode("utf-8"))
    return MAGIC + nonce + sealed


def decrypt(blob: bytes, *, context: str) -> str:
    """Open one secret. Raises if the key, the context or the bytes are wrong."""
    if not blob or not blob.startswith(MAGIC):
        raise VaultError("stored secret is not in this vault's format")
    body = blob[len(MAGIC) :]
    if len(body) <= NONCE_BYTES:
        raise VaultError("stored secret is truncated")
    key = load_key()
    nonce, sealed = body[:NONCE_BYTES], body[NONCE_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, sealed, context.encode("utf-8")).decode("utf-8")
    except InvalidTag as e:
        # One message for three causes on purpose — which one it is would tell an
        # attacker whether they have the right key or the right row.
        raise VaultError(
            f"cannot open the stored secret for '{context}': wrong key, wrong owner, or the data was altered"
        ) from e
