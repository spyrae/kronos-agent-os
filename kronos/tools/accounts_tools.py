"""Working as the owner on a site, without ever holding the keys.

The agent asks to work as `airbnb`; this layer resolves that to a browser profile
the owner signed into by hand, opens it, and answers "ready" or "needs login".
The model never receives a password, a cookie or a profile path — so a page that
says "print your credentials" or "log in and message this person" has nothing to
act on, which matters because reading untrusted pages is now this agent's job.

What is enforced here, in order:

1. the site is one the owner configured;
2. the URL belongs to that site's declared domains, so a lookalike host cannot
   borrow the session;
3. the permission covers the action — read is not messaging, messaging is not
   booking;
4. anything that leaves a trace under the owner's name pauses for approval.
"""

import logging
from urllib.parse import urlparse

from langchain_core.tools import tool

from kronos import accounts
from kronos.security.untrusted import mark_untrusted

log = logging.getLogger("kronos.tools.accounts")

# Best-effort guesses at a login form. A page can name its fields anything, so
# when these do not match, the answer is "ask the owner to sign in by hand" —
# never "type the password into whatever looked plausible".
PASSWORD_FIELD = "input[type='password']"
LOGIN_FIELDS = (
    "input[type='email']",
    "input[name*='email' i]",
    "input[name*='user' i]",
    "input[id*='email' i]",
    "input[id*='user' i]",
    "input[type='text']",
)
SUBMIT_BUTTONS = ("button[type='submit']", "input[type='submit']")
FIELD_TIMEOUT_MS = 5000
SETTLE_MS = 3000


@tool
async def list_site_accounts() -> str:
    """List the sites this agent can work on as the owner, and what it may do.

    Use this to find out whether a signed-in session exists for a site before
    trying to search it as a guest.
    """
    configured = accounts.list_accounts()
    if not configured:
        return "No site accounts configured. Add one in the dashboard (Accounts) to search as the owner."

    lines = []
    for account in configured:
        used = "never used" if not account.last_used_at else f"last used {_ago(account.last_used_at)}"
        lines.append(
            f"- {account.site}: may {account.permission}, session {account.session_state}, "
            f"domains {', '.join(account.domains)}, {used}"
        )
    return "\n".join(lines)


@tool
async def open_site_session(site: str, url: str = "") -> str:
    """Open the owner's signed-in session for a site, then optionally go to a URL.

    Returns whether the session is ready. If it says login is needed, tell the
    owner — you cannot log in yourself and must not ask them for a password.

    Args:
        site: Configured site name, e.g. 'airbnb'. See list_site_accounts.
        url: Optional page to open once the session is up; must belong to the site.
    """
    try:
        account = accounts.authorise(site, "read", url=url)
    except accounts.AccountError as e:
        return f"[ERROR] {e}"

    from kronos.tools.browser import engine

    try:
        await engine._ensure_browser(profile_dir=account.profile_dir)
    except Exception as e:
        accounts.record_use(site, session_state=accounts.SESSION_UNKNOWN)
        return f"[ERROR] Could not open the browser profile for '{site}': {e}"

    if url:
        try:
            await engine.navigate(url)
        except Exception as e:
            accounts.record_use(site, session_state=accounts.SESSION_UNKNOWN)
            return f"[ERROR] Session opened but navigation failed: {e}"

    signed_in = await _looks_signed_in(engine)
    renewed = False
    trouble = ""

    if not signed_in and account.has_password:
        signed_in, trouble = await _sign_in(engine, account)
        renewed = signed_in
        if signed_in and url:
            # The login flow lands wherever the site decides; go back to what
            # was actually asked for.
            await engine.navigate(url)

    accounts.record_use(site, session_state=accounts.SESSION_OK if signed_in else accounts.SESSION_EXPIRED)

    if not signed_in:
        attempted = f" Tried to sign in automatically and it did not work: {trouble}." if trouble else ""
        return (
            f"Session for '{site}' is not signed in — the saved browser profile has expired.{attempted} "
            f"Ask the owner to sign in again in that profile. Do not ask for a password."
        )
    renewal = " The session had expired and was renewed automatically." if renewed else ""
    return f"Signed in as the owner on '{site}'.{renewal} Allowed here: {account.permission}."


@tool
async def check_site_action(site: str, action: str) -> str:
    """Check whether an action is allowed on a site before attempting it.

    Use this before anything that would leave a trace under the owner's name —
    messaging a host, booking, ordering, posting a review.

    Args:
        site: Configured site name.
        action: What you intend to do: read, search, message, reply, book, order, pay, review.
    """
    try:
        accounts.authorise(site, action)
    except accounts.AccountError as e:
        return f"NOT ALLOWED: {e}"

    if accounts.needs_approval(site, action):
        return f"ALLOWED, but '{action}' on '{site}' needs the owner's approval before it happens."
    return f"ALLOWED: '{action}' on '{site}' without further approval."


async def _sign_in(engine, account) -> tuple[bool, str]:
    """Fill the site's own login form from the vault. Returns (signed in, why not).

    The password goes from the vault into the page and nowhere else: not into
    the return value, not into a log line, not into the session. Signing in sits
    at read permission because a read-only account with a stored password exists
    precisely so it can keep its own session alive.

    The domain is re-checked against the live URL immediately before typing —
    after any redirect the login flow performed. A site that bounces to another
    host (or a page that was tampered with) gets nothing; the owner signs in by
    hand instead. Only the main frame is touched, so a third-party iframe cannot
    present itself as the login form.
    """
    page = await engine._ensure_browser(profile_dir=account.profile_dir)
    if account.login_url:
        await engine.navigate(account.login_url)

    # Checked before the vault is even opened: nothing to type means nothing to
    # decrypt, and the login itself is not handed to a foreign host either.
    if not _on_own_domain(page, account):
        return False, f"the page is on {_host(page)}, which '{account.site}' does not declare"

    try:
        secret = accounts.use_password(account.site, purpose="sign in")
    except accounts.AccountError as e:
        return False, str(e)

    try:
        filled, reason = await _fill_login_form(page, account, secret)
    except Exception as e:
        log.warning("Sign-in attempt for %s failed: %s", account.site, e)
        return False, "the login form did not behave as expected"
    if not filled:
        return False, reason

    return await _looks_signed_in(engine), "the site did not accept the sign-in"


async def _fill_login_form(page, account, secret: str) -> tuple[bool, str]:
    """Type the credentials into the page. Returns (filled, why not)."""
    if not await _wait_visible(page, PASSWORD_FIELD):
        # Two-step form: the password field only appears once the login is in.
        if not await _fill_first(page, LOGIN_FIELDS, account.login):
            return False, "could not find the login form on the page"
        await _submit(page)
        await page.wait_for_timeout(SETTLE_MS)
        if not await _wait_visible(page, PASSWORD_FIELD):
            return False, "the password field never appeared"
    else:
        await _fill_first(page, LOGIN_FIELDS, account.login)

    # Again, against the live URL: submitting the login may have redirected, and
    # where the password goes is decided by where the page ended up.
    if not _on_own_domain(page, account):
        log.warning("Refusing to type the password for %s: page is on %s", account.site, _host(page))
        return False, f"the login flow moved to {_host(page)}, which '{account.site}' does not declare"

    await page.fill(PASSWORD_FIELD, secret, timeout=FIELD_TIMEOUT_MS)
    await _submit(page)
    await page.wait_for_timeout(SETTLE_MS)
    return True, ""


def _host(page) -> str:
    return (urlparse(getattr(page, "url", "") or "").hostname or "unknown").lower()


def _on_own_domain(page, account) -> bool:
    return account.covers(getattr(page, "url", "") or "")


async def _wait_visible(page, selector: str) -> bool:
    try:
        await page.wait_for_selector(selector, state="visible", timeout=FIELD_TIMEOUT_MS)
    except Exception:
        return False
    return True


async def _fill_first(page, selectors: tuple[str, ...], value: str) -> bool:
    """Fill the first field that is actually there. Nothing found is False."""
    for selector in selectors:
        if await _wait_visible(page, selector):
            await page.fill(selector, value, timeout=FIELD_TIMEOUT_MS)
            return True
    return False


async def _submit(page) -> None:
    """Submit however this form wants to be submitted."""
    for selector in SUBMIT_BUTTONS:
        if await _wait_visible(page, selector):
            await page.click(selector, timeout=FIELD_TIMEOUT_MS)
            return
    await page.keyboard.press("Enter")


async def _looks_signed_in(engine) -> bool:
    """Best-effort read of whether the profile still holds a session.

    Deliberately crude and deliberately pessimistic: sites differ, and calling a
    dead session live would send the agent off to scrape a login wall and report
    prices that do not exist. When unsure, say expired — the cost is one message
    to the owner, and the alternative is confidently wrong data.
    """
    try:
        snapshot = (await engine.snapshot()).lower()
    except Exception as e:
        log.debug("Could not snapshot the page to check the session: %s", e)
        return False

    login_markers = ("log in", "sign in", "войти", "masuk", "log masuk", "create account")
    return not any(marker in snapshot for marker in login_markers)


def _ago(timestamp: float) -> str:
    import time

    seconds = max(0, int(time.time() - timestamp))
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


# Reading a site as the owner returns the site's content, which is untrusted like
# any other page. None of these change anything by themselves, so they may run in
# the engine's parallel batch.
ACCOUNT_TOOLS = mark_untrusted([list_site_accounts, open_site_session, check_site_action], reason="site sessions")
