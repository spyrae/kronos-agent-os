"""`kaos vault` — the two questions an owner has about their own key.

Where does it live, and does replacing it quietly destroy what is stored? The
second is the one worth a test: a key file overwritten by accident makes every
saved password unreadable, and nothing else would tell them.
"""

import json

import pytest

from kronos import vault
from kronos.accounts import save_account
from kronos.cli import main
from kronos.config import settings


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "vault_key", "")
    monkeypatch.setattr(settings, "vault_key_path", str(tmp_path / "vault.key"))
    import kronos.db as _db

    _db._instances.clear()
    yield
    _db._instances.clear()


def test_status_without_a_key_fails_and_says_what_to_run(capsys):
    assert main(["vault", "status"]) == 1

    assert "kaos vault init" in capsys.readouterr().out


def test_init_creates_a_usable_key(capsys):
    assert main(["vault", "init"]) == 0
    assert main(["vault", "status"]) == 0

    out = capsys.readouterr().out
    assert "mode 0600" in out
    assert "Back it up" in out, "losing the key loses every password; say so"


def test_init_refuses_to_destroy_an_existing_key(capsys):
    main(["vault", "init"])
    before = vault.default_key_path().read_text()

    assert main(["vault", "init"]) == 1

    assert vault.default_key_path().read_text() == before
    assert "--replace" in capsys.readouterr().out


def test_replacing_a_key_is_possible_when_asked():
    main(["vault", "init"])
    before = vault.default_key_path().read_text()

    assert main(["vault", "init", "--replace"]) == 0

    assert vault.default_key_path().read_text() != before


def test_init_does_not_write_a_file_when_the_environment_holds_the_key(monkeypatch, capsys):
    monkeypatch.setattr(settings, "vault_key", vault.generate_key())

    assert main(["vault", "init"]) == 0

    assert not vault.default_key_path().exists()
    assert "already set in the environment" in capsys.readouterr().out


def test_status_names_the_accounts_that_depend_on_the_key(capsys):
    main(["vault", "init"])
    save_account(site="airbnb", domains=["airbnb.com"], login="roman@example.com", password="hunter2")
    save_account(site="booking", domains=["booking.com"], profile_dir="/tmp/p")
    capsys.readouterr()  # drop what init printed

    assert main(["vault", "status", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["accounts_with_password"] == ["airbnb"]
    assert payload["key_source"] == vault.SOURCE_FILE
    assert payload["usable"] is True
