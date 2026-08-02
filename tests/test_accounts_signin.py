"""Signing back in by itself — and refusing to, when the page is not the site's.

The password leaves the vault here and goes into a web page, which makes this
the one place in the codebase where being wrong about a hostname costs the
owner their credentials. So the tests are mostly about not typing: a redirect
to another host, a login flow that wanders off mid-way, a form that is not
there. The successful case matters too, but it is the cheap half.
"""

import pytest

from kronos import vault
from kronos.accounts import SESSION_EXPIRED, SESSION_OK, get_account, save_account
from kronos.config import settings
from kronos.tools import accounts_tools
from kronos.tools.accounts_tools import open_site_session

SECRET = "correct-horse-battery"


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "vault_key", vault.generate_key())
    monkeypatch.setattr(settings, "vault_key_path", str(tmp_path / "vault.key"))
    import kronos.db as _db

    _db._instances.clear()
    yield
    _db._instances.clear()


class FakePage:
    """A login page, reduced to what the sign-in flow actually touches."""

    def __init__(self, *, url: str, fields: set[str], url_after_login: str = ""):
        self.url = url
        self._fields = fields
        self._url_after_login = url_after_login
        self.typed: dict[str, str] = {}
        self.submits = 0

    async def wait_for_selector(self, selector: str, state: str = "visible", timeout: int = 0):
        if selector not in self._fields:
            raise TimeoutError(f"no {selector}")
        return object()

    async def fill(self, selector: str, value: str, timeout: int = 0) -> None:
        if selector not in self._fields:
            raise TimeoutError(f"no {selector}")
        self.typed[selector] = value

    async def click(self, selector: str, timeout: int = 0) -> None:
        self.submits += 1
        self._advance()

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    @property
    def keyboard(self):
        return self

    async def press(self, key: str) -> None:
        self.submits += 1
        self._advance()

    def _advance(self) -> None:
        """A two-step form reveals the password field once the login is in."""
        if accounts_tools.PASSWORD_FIELD not in self._fields and self.typed:
            self._fields = self._fields | {accounts_tools.PASSWORD_FIELD}
        if self._url_after_login:
            self.url = self._url_after_login
            self._url_after_login = ""


class FakeEngine:
    """The browser: signed out until the form is submitted, then signed in."""

    def __init__(self, page: FakePage, *, accepts_login: bool = True):
        self.page = page
        self.accepts_login = accepts_login
        self.visited: list[str] = []

    async def _ensure_browser(self, profile_dir=None):
        return self.page

    async def navigate(self, url: str) -> str:
        self.visited.append(url)
        self.page.url = url
        return "ok"

    async def snapshot(self) -> str:
        if self.page.submits and self.accepts_login:
            return "Wishlists · Trips · Messages"
        return "Log in or sign up to continue"


@pytest.fixture
def engine(monkeypatch):
    def install(page: FakePage, *, accepts_login: bool = True) -> FakeEngine:
        fake = FakeEngine(page, accepts_login=accepts_login)
        import kronos.tools.browser.engine as real

        monkeypatch.setattr(real, "_ensure_browser", fake._ensure_browser)
        monkeypatch.setattr(real, "navigate", fake.navigate)
        monkeypatch.setattr(real, "snapshot", fake.snapshot)
        return fake

    return install


def _account(**overrides):
    payload = {
        "site": "airbnb",
        "domains": ["airbnb.com"],
        "login": "roman@example.com",
        "profile_dir": "/tmp/profiles/airbnb",
        "password": SECRET,
    }
    return save_account(**{**payload, **overrides})


def _one_step_page(url="https://www.airbnb.com/login"):
    return FakePage(url=url, fields={accounts_tools.PASSWORD_FIELD, "input[type='email']"})


# --- signing in ---------------------------------------------------------------


async def test_an_expired_session_is_renewed_without_asking(engine):
    _account()
    page = _one_step_page()
    engine(page)

    out = await open_site_session.ainvoke({"site": "airbnb"})

    assert "Signed in as the owner" in out
    assert "renewed automatically" in out
    assert page.typed[accounts_tools.PASSWORD_FIELD] == SECRET
    assert page.typed["input[type='email']"] == "roman@example.com"
    assert get_account("airbnb").session_state == SESSION_OK


async def test_the_password_never_appears_in_what_the_model_reads(engine):
    _account()
    engine(_one_step_page())

    out = await open_site_session.ainvoke({"site": "airbnb"})

    assert SECRET not in out


async def test_a_two_step_form_is_handled(engine):
    """The password field only exists after the login is submitted."""
    _account()
    page = FakePage(url="https://www.airbnb.com/login", fields={"input[type='email']"})
    engine(page)

    out = await open_site_session.ainvoke({"site": "airbnb"})

    assert "Signed in as the owner" in out
    assert page.typed[accounts_tools.PASSWORD_FIELD] == SECRET


async def test_it_returns_to_the_page_that_was_asked_for(engine):
    _account()
    fake = engine(_one_step_page())

    await open_site_session.ainvoke({"site": "airbnb", "url": "https://www.airbnb.com/s/Bali"})

    assert fake.visited[-1] == "https://www.airbnb.com/s/Bali"


async def test_the_login_url_is_used_when_configured(engine):
    _account(login_url="https://www.airbnb.com/login")
    fake = engine(_one_step_page(url="https://www.airbnb.com/"))

    await open_site_session.ainvoke({"site": "airbnb"})

    assert "https://www.airbnb.com/login" in fake.visited


# --- refusing to type ---------------------------------------------------------


async def test_nothing_is_typed_on_a_host_the_account_does_not_declare(engine):
    """A lookalike domain is what a tampered page or a stale link would land on."""
    _account()
    page = _one_step_page(url="https://airbnb.com.evil.test/login")
    engine(page)

    out = await open_site_session.ainvoke({"site": "airbnb"})

    assert page.typed == {}, "no credential may reach a host outside the account"
    assert "does not declare" in out
    assert get_account("airbnb").session_state == SESSION_EXPIRED


async def test_the_password_is_withheld_when_the_flow_wanders_off_mid_way(engine):
    """The login went in on the right host; the next step did not stay there."""
    _account()
    page = FakePage(
        url="https://www.airbnb.com/login",
        fields={"input[type='email']"},
        url_after_login="https://evil.test/continue",
    )
    engine(page)

    out = await open_site_session.ainvoke({"site": "airbnb"})

    assert accounts_tools.PASSWORD_FIELD not in page.typed
    assert "moved to evil.test" in out


async def test_a_missing_form_is_reported_not_guessed_at(engine):
    _account()
    page = FakePage(url="https://www.airbnb.com/login", fields=set())
    engine(page)

    out = await open_site_session.ainvoke({"site": "airbnb"})

    assert "could not find the login form" in out
    assert "Do not ask for a password" in out


async def test_a_rejected_sign_in_is_reported_honestly(engine):
    """Reporting success here would send the agent off to scrape a login wall."""
    _account()
    page = _one_step_page()
    engine(page, accepts_login=False)

    out = await open_site_session.ainvoke({"site": "airbnb"})

    assert "not signed in" in out
    assert "did not accept the sign-in" in out
    assert get_account("airbnb").session_state == SESSION_EXPIRED


async def test_without_a_stored_password_it_asks_the_owner(engine):
    _account(password="")
    page = _one_step_page()
    engine(page)

    out = await open_site_session.ainvoke({"site": "airbnb"})

    assert page.typed == {}, "an account with no password must not type anything"
    assert "Ask the owner to sign in again" in out


async def test_signing_in_is_recorded_in_the_audit_trail(engine, tmp_path):
    _account()
    engine(_one_step_page())

    await open_site_session.ainvoke({"site": "airbnb"})

    trail = (tmp_path / "logs" / "credentials.jsonl").read_text()
    assert SECRET not in trail
    assert '"purpose": "sign in"' in trail
