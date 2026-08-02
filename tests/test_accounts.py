"""Acting as the owner on a site, without holding the keys.

The tests that matter here are about what cannot happen: a lookalike domain
borrowing the session, a read-only account messaging someone, an unclassified
action being assumed harmless, and — the reason for the whole indirection — a
secret or a profile path reaching the model.
"""

import pytest

from kronos.accounts import (
    METHOD_PASSWORD,
    METHOD_PROFILE,
    PERMISSION_FULL,
    PERMISSION_MESSAGE,
    PERMISSION_READ,
    SESSION_EXPIRED,
    SESSION_OK,
    AccountError,
    SiteAccount,
    authorise,
    delete_account,
    get_account,
    list_accounts,
    needs_approval,
    record_use,
    save_account,
)
from kronos.config import settings


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    import kronos.db as _db

    _db._instances.clear()
    yield
    _db._instances.clear()


def _airbnb(**overrides):
    payload = {
        "site": "airbnb",
        "domains": ["airbnb.com", "airbnb.ru"],
        "method": METHOD_PROFILE,
        "login": "roman@example.com",
        "profile_dir": "/tmp/profiles/airbnb",
        "permission": PERMISSION_READ,
    }
    return save_account(**{**payload, **overrides})


# --- configuring --------------------------------------------------------------


def test_an_account_round_trips():
    _airbnb(notes="личный аккаунт")

    account = get_account("airbnb")

    assert account.domains == ["airbnb.com", "airbnb.ru"]
    assert account.login == "roman@example.com"
    assert account.permission == PERMISSION_READ
    assert account.approval_required is True
    assert account.notes == "личный аккаунт"


def test_saving_twice_updates_rather_than_duplicates():
    _airbnb()
    _airbnb(permission=PERMISSION_MESSAGE, notes="теперь можно писать")

    assert len(list_accounts()) == 1
    assert get_account("airbnb").permission == PERMISSION_MESSAGE


def test_an_account_needs_domains():
    with pytest.raises(AccountError, match="at least one domain"):
        save_account(site="airbnb", domains=[], profile_dir="/tmp/p")


def test_a_profile_account_needs_a_profile_directory():
    with pytest.raises(AccountError, match="profile_dir"):
        save_account(site="airbnb", domains=["airbnb.com"], profile_dir="")


def test_an_unknown_permission_is_refused():
    with pytest.raises(AccountError, match="unknown permission"):
        save_account(site="a", domains=["a.com"], profile_dir="/tmp/p", permission="root")


def test_stored_passwords_are_refused_until_the_vault_exists():
    """A half-built vault that falls back to plaintext is worse than none."""
    with pytest.raises(AccountError, match="not enabled yet"):
        save_account(site="airbnb", domains=["airbnb.com"], method=METHOD_PASSWORD, login="x")


def test_a_missing_account_lists_what_is_configured():
    _airbnb()

    with pytest.raises(AccountError, match="known: airbnb"):
        get_account("booking")


def test_deleting_an_account_removes_it():
    _airbnb()

    assert delete_account("airbnb") is True
    assert list_accounts() == []
    assert delete_account("airbnb") is False


# --- which URLs the session may be used on ------------------------------------


@pytest.mark.parametrize(
    "url,covered",
    [
        ("https://www.airbnb.com/rooms/1", True),
        ("https://airbnb.com/s/Bali", True),
        ("https://secure.airbnb.ru/x", True),
        ("https://booking.com/x", False),
        ("https://airbnb.com.evil.test/x", False),  # the lookalike a page would point at
        ("https://notairbnb.com/x", False),
        ("not a url", False),
    ],
)
def test_domain_matching_is_on_host_boundaries(url, covered):
    account = SiteAccount(site="airbnb", domains=["airbnb.com", "airbnb.ru"])

    assert account.covers(url) is covered


def test_authorise_refuses_a_url_outside_the_account():
    _airbnb()

    with pytest.raises(AccountError, match="not one of the domains"):
        authorise("airbnb", "read", url="https://airbnb.com.evil.test/rooms/1")


# --- what the agent may do ----------------------------------------------------


@pytest.mark.parametrize(
    "permission,action,allowed",
    [
        (PERMISSION_READ, "read", True),
        (PERMISSION_READ, "search", True),
        (PERMISSION_READ, "message", False),
        (PERMISSION_READ, "book", False),
        (PERMISSION_MESSAGE, "message", True),
        (PERMISSION_MESSAGE, "reply", True),
        (PERMISSION_MESSAGE, "book", False),
        (PERMISSION_FULL, "book", True),
        (PERMISSION_FULL, "pay", True),
    ],
)
def test_permission_levels(permission, action, allowed):
    account = SiteAccount(site="x", domains=["x.com"], permission=permission)

    assert account.allows(action) is allowed


def test_an_unclassified_action_is_refused_not_assumed_harmless():
    """The list of things one can do while signed in only grows."""
    account = SiteAccount(site="x", domains=["x.com"], permission=PERMISSION_FULL)

    assert account.allows("transfer_ownership") is False


def test_authorise_explains_how_to_widen_it():
    _airbnb()

    with pytest.raises(AccountError, match="dashboard"):
        authorise("airbnb", "message")


# --- approvals ----------------------------------------------------------------


def test_reading_never_needs_approval():
    _airbnb(permission=PERMISSION_FULL)

    assert needs_approval("airbnb", "read") is False


def test_anything_leaving_a_trace_needs_approval_by_default():
    _airbnb(permission=PERMISSION_FULL)

    assert needs_approval("airbnb", "message") is True
    assert needs_approval("airbnb", "book") is True


def test_approval_can_be_waived_per_site():
    _airbnb(permission=PERMISSION_FULL, approval_required=False)

    assert needs_approval("airbnb", "message") is False


def test_an_unknown_site_needs_approval():
    """Failing open here would make a typo into a free pass."""
    assert needs_approval("nowhere", "book") is True


# --- session bookkeeping ------------------------------------------------------


def test_use_is_recorded_with_what_was_learned():
    _airbnb()

    record_use("airbnb", session_state=SESSION_OK)

    account = get_account("airbnb")
    assert account.session_state == SESSION_OK
    assert account.last_used_at > 0


def test_an_unknown_session_state_is_refused():
    _airbnb()

    with pytest.raises(AccountError, match="unknown session state"):
        record_use("airbnb", session_state="probably-fine")


# --- the tool surface ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_listing_never_shows_a_profile_path_or_a_login():
    """What reaches the model is a handle and a permission, nothing more."""
    from kronos.tools.accounts_tools import list_site_accounts

    _airbnb()

    out = await list_site_accounts.ainvoke({})

    assert "airbnb" in out
    assert "may read" in out
    assert "/tmp/profiles/airbnb" not in out, "a profile path is a secret-adjacent detail"
    assert "roman@example.com" not in out, "the login is not the model's business"


@pytest.mark.asyncio
async def test_no_accounts_configured_says_where_to_add_one():
    from kronos.tools.accounts_tools import list_site_accounts

    assert "dashboard" in (await list_site_accounts.ainvoke({})).lower()


@pytest.mark.asyncio
async def test_opening_a_session_for_an_unknown_site_is_an_error():
    from kronos.tools.accounts_tools import open_site_session

    out = await open_site_session.ainvoke({"site": "booking"})

    assert out.startswith("[ERROR]")


@pytest.mark.asyncio
async def test_opening_a_session_refuses_a_foreign_url(monkeypatch):
    from kronos.tools.accounts_tools import open_site_session

    _airbnb()

    async def must_not_start(profile_dir=None):
        raise AssertionError("the browser must not start for a refused URL")

    monkeypatch.setattr("kronos.tools.browser.engine._ensure_browser", must_not_start)

    out = await open_site_session.ainvoke({"site": "airbnb", "url": "https://airbnb.com.evil.test/x"})

    assert out.startswith("[ERROR]")
    assert "not one of the domains" in out


@pytest.mark.asyncio
async def test_a_live_session_reports_ready_and_is_recorded(monkeypatch):
    from kronos.tools.accounts_tools import open_site_session

    _airbnb()
    opened: dict = {}

    async def fake_browser(profile_dir=None):
        opened["profile_dir"] = profile_dir

    monkeypatch.setattr("kronos.tools.browser.engine._ensure_browser", fake_browser)
    monkeypatch.setattr("kronos.tools.browser.engine.navigate", _async("Airbnb"))
    monkeypatch.setattr("kronos.tools.browser.engine.snapshot", _async("Wishlists · Trips · Messages"))

    out = await open_site_session.ainvoke({"site": "airbnb", "url": "https://www.airbnb.com/s/Bali"})

    assert "Signed in as the owner" in out
    assert opened["profile_dir"] == "/tmp/profiles/airbnb", "the profile is used, not exposed"
    assert "/tmp/profiles/airbnb" not in out
    assert get_account("airbnb").session_state == SESSION_OK


@pytest.mark.asyncio
async def test_an_expired_session_says_so_and_forbids_asking_for_a_password(monkeypatch):
    from kronos.tools.accounts_tools import open_site_session

    _airbnb()
    monkeypatch.setattr("kronos.tools.browser.engine._ensure_browser", _async(None))
    monkeypatch.setattr("kronos.tools.browser.engine.navigate", _async("Airbnb"))
    monkeypatch.setattr("kronos.tools.browser.engine.snapshot", _async("Log in or sign up to continue"))

    out = await open_site_session.ainvoke({"site": "airbnb", "url": "https://www.airbnb.com/"})

    assert "not signed in" in out
    assert "Do not ask for a password" in out
    assert get_account("airbnb").session_state == SESSION_EXPIRED


@pytest.mark.asyncio
async def test_a_snapshot_failure_counts_as_expired(monkeypatch):
    """Confidently reporting prices from a login wall is the worse failure."""
    from kronos.tools.accounts_tools import open_site_session

    _airbnb()
    monkeypatch.setattr("kronos.tools.browser.engine._ensure_browser", _async(None))

    async def broken_snapshot():
        raise RuntimeError("page gone")

    monkeypatch.setattr("kronos.tools.browser.engine.snapshot", broken_snapshot)

    out = await open_site_session.ainvoke({"site": "airbnb"})

    assert "not signed in" in out


@pytest.mark.asyncio
async def test_checking_an_action_reports_the_approval_requirement():
    from kronos.tools.accounts_tools import check_site_action

    _airbnb(permission=PERMISSION_MESSAGE)

    assert "NOT ALLOWED" in await check_site_action.ainvoke({"site": "airbnb", "action": "book"})
    assert "needs the owner's approval" in await check_site_action.ainvoke({"site": "airbnb", "action": "message"})
    assert "without further approval" in await check_site_action.ainvoke({"site": "airbnb", "action": "read"})


def _async(value):
    async def _call(*args, **kwargs):
        return value

    return _call
