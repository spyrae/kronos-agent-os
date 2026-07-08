"""Session-based auth with login/password."""

import secrets
import time

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from dashboard.config import DASHBOARD_PASSWORD, DASHBOARD_USERNAME

security = HTTPBearer(auto_error=False)

# Active sessions: token -> expiry timestamp
_sessions: dict[str, float] = {}
SESSION_TTL = 7 * 24 * 3600  # 7 days


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


async def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    """Verify Bearer session token."""
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = credentials.credentials
    expiry = _sessions.get(token)

    if not expiry or expiry < time.time():
        _sessions.pop(token, None)
        raise HTTPException(status_code=401, detail="Session expired")

    return token
