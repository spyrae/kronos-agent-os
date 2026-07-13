"""Session-based auth with login/password.

The session token lives in an ``HttpOnly``, ``SameSite=Strict`` cookie, not in
JavaScript-readable storage: an XSS on the dashboard (which renders agent
output) cannot exfiltrate it, and it never travels in a URL/query string where
proxy and access logs would capture it. A ``Bearer`` header is still accepted
so scripts/curl can authenticate. Login is rate-limited per client IP to blunt
password brute-forcing.
"""

import secrets
import time

from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from dashboard.config import DASHBOARD_PASSWORD, DASHBOARD_USERNAME

security = HTTPBearer(auto_error=False)

# Name of the session cookie set on login and read on every authed request.
COOKIE_NAME = "kronos_session"

# Active sessions: token -> expiry timestamp
_sessions: dict[str, float] = {}
SESSION_TTL = 7 * 24 * 3600  # 7 days

# Failed-login throttling (brute-force mitigation), keyed by client IP.
LOGIN_WINDOW_SECONDS = 900  # 15 minutes
LOGIN_MAX_FAILURES = 10  # failures within the window before lockout
_login_failures: dict[str, list[float]] = {}


def create_session() -> str:
    """Create a new session token."""
    token = secrets.token_hex(32)
    _sessions[token] = time.time() + SESSION_TTL
    # Clean expired
    now = time.time()
    expired = [k for k, v in _sessions.items() if v < now]
    for k in expired:
        del _sessions[k]
    return token


def verify_credentials(username: str, password: str) -> bool:
    """Check username and password in constant time (avoids timing attacks).

    Both comparisons are evaluated before the ``and`` so short-circuiting
    doesn't leak which field mismatched via response timing.
    """
    user_ok = secrets.compare_digest(username.encode(), DASHBOARD_USERNAME.encode())
    pass_ok = secrets.compare_digest(password.encode(), DASHBOARD_PASSWORD.encode())
    return user_ok and pass_ok


def verify_session_token(token: str) -> bool:
    """Check that a session token exists and has not expired."""
    if not token:
        return False
    expiry = _sessions.get(token)
    if not expiry or expiry < time.time():
        _sessions.pop(token, None)
        return False
    return True


def invalidate_session(token: str) -> None:
    """Drop a session token (logout) so a stolen cookie stops working."""
    _sessions.pop(token, None)


def login_retry_after(ip: str) -> int:
    """Seconds until ``ip`` may retry login, or 0 if it is not locked out."""
    now = time.time()
    recent = [t for t in _login_failures.get(ip, []) if t > now - LOGIN_WINDOW_SECONDS]
    if len(recent) < LOGIN_MAX_FAILURES:
        return 0
    return max(1, int(recent[0] + LOGIN_WINDOW_SECONDS - now))


def record_login_failure(ip: str) -> None:
    """Record one failed login for ``ip``, pruning entries outside the window."""
    now = time.time()
    recent = [t for t in _login_failures.get(ip, []) if t > now - LOGIN_WINDOW_SECONDS]
    recent.append(now)
    _login_failures[ip] = recent


def clear_login_failures(ip: str) -> None:
    """Reset the failure count for ``ip`` after a successful login."""
    _login_failures.pop(ip, None)


def set_session_cookie(response: Response, token: str, *, secure: bool) -> None:
    """Attach the session cookie: HttpOnly + SameSite=Strict (+ Secure on TLS)."""
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Expire the session cookie (logout)."""
    response.delete_cookie(key=COOKIE_NAME, path="/", samesite="strict")


async def verify_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    """Verify the session token from the HttpOnly cookie or a Bearer header."""
    token = request.cookies.get(COOKIE_NAME) or (credentials.credentials if credentials else "")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not verify_session_token(token):
        raise HTTPException(status_code=401, detail="Session expired")
    return token
