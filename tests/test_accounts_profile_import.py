"""Adopting a browser profile signed in somewhere with a screen.

The gap this closes: the whole site-accounts design says "sign in by hand once",
and the machine the agent runs on is a headless server. So the profile is made on
a laptop and copied over — and the copy has to be checked, because the failure of
pointing an account at the wrong directory is late and unreadable: the browser
starts on an empty profile, the site shows its login wall, and the account
reports an expired session forever.
"""

import json
import os

import pytest

from kronos import accounts, vault
from kronos.cli import main
from kronos.config import settings


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "data" / "session.db"))
    monkeypatch.setattr(settings, "vault_key", vault.generate_key())
    import kronos.db as _db

    _db._instances.clear()
    yield
    _db._instances.clear()


@pytest.fixture
def account():
    return accounts.save_account(
        site="airbnb",
        domains=["airbnb.com"],
        login="roman@example.com",
        profile_dir="/tmp/placeholder",
    )


@pytest.fixture
def signed_in_profile(tmp_path):
    """What Chromium leaves behind after someone logs in."""
    profile = tmp_path / "from-laptop"
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Cookies").write_text("session=abc")
    (profile / "Local State").write_text("{}")
    return profile


# --- what counts as a profile -------------------------------------------------


@pytest.mark.parametrize("marker", ["Default", "Preferences", "Local State"])
def test_a_directory_with_any_chromium_marker_is_a_profile(tmp_path, marker):
    candidate = tmp_path / marker.replace(" ", "-")
    candidate.mkdir()
    (candidate / marker).write_text("x")

    assert accounts.looks_like_profile(candidate) is True


def test_an_ordinary_directory_is_not(tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "todo.md").write_text("x")

    assert accounts.looks_like_profile(tmp_path / "notes") is False


# --- importing ----------------------------------------------------------------


def test_importing_copies_the_profile_and_points_the_account_at_it(account, signed_in_profile, tmp_path):
    imported = accounts.import_profile("airbnb", str(signed_in_profile))

    landed = tmp_path / "data" / "browser-profiles" / "airbnb"
    assert imported.profile_dir == str(landed)
    assert (landed / "Default" / "Cookies").read_text() == "session=abc"


def test_the_copy_is_private_because_it_holds_live_cookies(account, signed_in_profile, tmp_path):
    """A profile readable by others is the same leak as a readable key file."""
    accounts.import_profile("airbnb", str(signed_in_profile))

    mode = os.stat(tmp_path / "data" / "browser-profiles" / "airbnb").st_mode & 0o777

    assert mode == 0o700


def test_the_original_is_left_alone(account, signed_in_profile):
    accounts.import_profile("airbnb", str(signed_in_profile))

    assert (signed_in_profile / "Default" / "Cookies").exists()


def test_importing_twice_replaces_rather_than_merges(account, signed_in_profile, tmp_path):
    """A stale cookie left from the previous profile is a session nobody can explain."""
    accounts.import_profile("airbnb", str(signed_in_profile))
    landed = tmp_path / "data" / "browser-profiles" / "airbnb"
    (landed / "Default" / "Stale").write_text("old")

    accounts.import_profile("airbnb", str(signed_in_profile))

    assert not (landed / "Default" / "Stale").exists()


def test_a_directory_that_is_not_a_profile_is_refused(account, tmp_path):
    (tmp_path / "downloads").mkdir()

    with pytest.raises(accounts.AccountError, match="does not look like a browser profile"):
        accounts.import_profile("airbnb", str(tmp_path / "downloads"))


def test_a_path_that_does_not_exist_is_refused(account, tmp_path):
    with pytest.raises(accounts.AccountError, match="not a directory"):
        accounts.import_profile("airbnb", str(tmp_path / "nowhere"))


def test_importing_into_itself_is_refused(account, signed_in_profile, tmp_path):
    """Otherwise the directory is deleted and then copied from — losing the profile."""
    accounts.import_profile("airbnb", str(signed_in_profile))
    landed = tmp_path / "data" / "browser-profiles" / "airbnb"

    with pytest.raises(accounts.AccountError, match="already this account's"):
        accounts.import_profile("airbnb", str(landed))

    assert (landed / "Default" / "Cookies").exists()


def test_importing_for_an_unknown_site_says_which_exist(signed_in_profile):
    with pytest.raises(accounts.AccountError, match="known:"):
        accounts.import_profile("booking", str(signed_in_profile))


# --- the command --------------------------------------------------------------


def test_the_command_reports_what_it_did_and_how_to_check(account, signed_in_profile, capsys):
    assert main(["accounts", "import-profile", "airbnb", str(signed_in_profile)]) == 0

    out = capsys.readouterr().out
    assert "0700" in out, "the owner should learn the copy is private"
    assert "signed-in session" in out, "and how to confirm it worked"


def test_the_command_fails_loudly_on_a_wrong_directory(account, tmp_path, capsys):
    (tmp_path / "nope").mkdir()

    assert main(["accounts", "import-profile", "airbnb", str(tmp_path / "nope")]) == 1
    assert "does not look like" in capsys.readouterr().out


def test_listing_accounts_never_shows_the_profile_path_or_a_password(account, capsys):
    accounts.set_password("airbnb", "hunter2")
    capsys.readouterr()

    assert main(["accounts", "list"]) == 0

    out = capsys.readouterr().out
    assert "airbnb" in out
    assert "password stored" in out
    assert "hunter2" not in out
    assert "/tmp/placeholder" not in out, "a profile path is secret-adjacent"


def test_listing_with_nothing_configured_points_at_the_dashboard(capsys):
    assert main(["accounts", "list"]) == 0

    assert "dashboard" in capsys.readouterr().out.lower()


def test_list_json_is_machine_readable(account, capsys):
    assert main(["accounts", "list", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["site"] == "airbnb"
    assert payload[0]["has_password"] is False
