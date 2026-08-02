"""The vault: a secret goes in, and only the right caller gets it back.

The tests that matter are the refusals — no key, a truncated key, a key file
anyone can read, a blob moved to another owner, a blob someone edited. Each of
those is a way a vault quietly stops being one.
"""

import base64
import os

import pytest

from kronos import vault
from kronos.config import settings


@pytest.fixture(autouse=True)
def isolated_key(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "vault_key", "")
    monkeypatch.setattr(settings, "vault_key_path", str(tmp_path / "vault.key"))
    yield


def _with_key(monkeypatch) -> str:
    key = vault.generate_key()
    monkeypatch.setattr(settings, "vault_key", key)
    return key


# --- storing and opening ------------------------------------------------------


def test_a_secret_round_trips(monkeypatch):
    _with_key(monkeypatch)

    blob = vault.encrypt("hunter2", context="airbnb")

    assert vault.decrypt(blob, context="airbnb") == "hunter2"


def test_the_stored_bytes_do_not_contain_the_secret(monkeypatch):
    _with_key(monkeypatch)

    blob = vault.encrypt("hunter2", context="airbnb")

    assert b"hunter2" not in blob


def test_encrypting_twice_gives_different_bytes(monkeypatch):
    """Equal ciphertexts would tell anyone reading the DB which accounts share a password."""
    _with_key(monkeypatch)

    assert vault.encrypt("same", context="a") != vault.encrypt("same", context="a")


def test_an_empty_secret_is_refused(monkeypatch):
    _with_key(monkeypatch)

    with pytest.raises(vault.VaultError, match="empty secret"):
        vault.encrypt("", context="airbnb")


# --- refusals -----------------------------------------------------------------


def test_a_secret_does_not_open_under_another_owner(monkeypatch):
    """A blob moved to another row must not unlock under that row's permissions."""
    _with_key(monkeypatch)

    blob = vault.encrypt("hunter2", context="airbnb")

    with pytest.raises(vault.VaultError, match="wrong key, wrong owner"):
        vault.decrypt(blob, context="booking")


def test_an_altered_secret_is_refused(monkeypatch):
    _with_key(monkeypatch)
    blob = bytearray(vault.encrypt("hunter2", context="airbnb"))

    blob[-1] ^= 0x01

    with pytest.raises(vault.VaultError):
        vault.decrypt(bytes(blob), context="airbnb")


def test_another_key_does_not_open_it(monkeypatch):
    _with_key(monkeypatch)
    blob = vault.encrypt("hunter2", context="airbnb")

    monkeypatch.setattr(settings, "vault_key", vault.generate_key())

    with pytest.raises(vault.VaultError):
        vault.decrypt(blob, context="airbnb")


@pytest.mark.parametrize("blob", [b"", b"plaintext", b"kaosv1short"])
def test_a_blob_that_is_not_ours_is_refused(monkeypatch, blob):
    _with_key(monkeypatch)

    with pytest.raises(vault.VaultError):
        vault.decrypt(blob, context="airbnb")


# --- the key -----------------------------------------------------------------


def test_without_a_key_nothing_is_stored():
    """The point of the whole module: no key, no write — never a plaintext fallback."""
    with pytest.raises(vault.VaultError, match="no vault key configured"):
        vault.encrypt("hunter2", context="airbnb")


def test_a_truncated_key_is_refused_loudly(monkeypatch):
    """Silently accepting a short key is how ciphertext becomes worthless unnoticed."""
    monkeypatch.setattr(settings, "vault_key", base64.urlsafe_b64encode(b"tooshort").decode())

    with pytest.raises(vault.VaultError, match="expected 32"):
        vault.load_key()


def test_a_key_that_is_not_base64_is_refused(monkeypatch):
    monkeypatch.setattr(settings, "vault_key", "!!! not a key !!!")

    with pytest.raises(vault.VaultError, match="not valid base64"):
        vault.load_key()


def test_an_empty_key_file_is_refused():
    vault.default_key_path().write_text("")
    os.chmod(vault.default_key_path(), 0o600)

    with pytest.raises(vault.VaultError, match="is empty"):
        vault.load_key()


@pytest.mark.skipif(os.name != "posix", reason="file modes are a posix notion")
def test_a_key_file_others_can_read_is_refused():
    path = vault.create_key_file()
    os.chmod(path, 0o644)

    with pytest.raises(vault.VaultError, match="chmod 600"):
        vault.load_key()


def test_a_created_key_file_is_private_and_works():
    path = vault.create_key_file()

    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    assert vault.decrypt(vault.encrypt("s", context="c"), context="c") == "s"


def test_creating_a_key_refuses_to_destroy_the_existing_one():
    vault.create_key_file()

    with pytest.raises(vault.VaultError, match="refusing to replace"):
        vault.create_key_file()


def test_replacing_a_key_is_possible_when_asked():
    first = vault.create_key_file().read_text()

    vault.create_key_file(overwrite=True)

    assert vault.default_key_path().read_text() != first


def test_the_environment_key_wins_over_the_file(monkeypatch):
    """Rotating by exporting a key must not silently keep using the old file."""
    vault.create_key_file()
    file_blob = vault.encrypt("from-file", context="c")

    _with_key(monkeypatch)

    assert vault.key_source() == vault.SOURCE_ENV
    with pytest.raises(vault.VaultError):
        vault.decrypt(file_blob, context="c")


def test_key_source_reports_where_the_trust_sits(monkeypatch):
    assert vault.key_source() == vault.SOURCE_NONE
    assert vault.available() is False

    vault.create_key_file()
    assert vault.key_source() == vault.SOURCE_FILE
    assert vault.available() is True


def test_a_broken_key_counts_as_no_key(monkeypatch):
    """`available()` answers "can I store a secret", not "is something configured"."""
    monkeypatch.setattr(settings, "vault_key", "garbage")

    assert vault.available() is False
