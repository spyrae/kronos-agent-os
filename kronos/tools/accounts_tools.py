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

from langchain_core.tools import tool

from kronos import accounts
from kronos.security.untrusted import mark_untrusted

log = logging.getLogger("kronos.tools.accounts")


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

    if account.method != accounts.METHOD_PROFILE:
        return f"[ERROR] Account '{site}' is not usable yet: only browser-profile accounts are supported."

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
    accounts.record_use(site, session_state=accounts.SESSION_OK if signed_in else accounts.SESSION_EXPIRED)

    if not signed_in:
        return (
            f"Session for '{site}' is not signed in — the saved browser profile has expired. "
            f"Ask the owner to sign in again in that profile. Do not ask for a password."
        )
    return f"Signed in as the owner on '{site}'. Allowed here: {account.permission}."


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
