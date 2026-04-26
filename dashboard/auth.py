"""Session-based auth with login/password."""

import hashlib
import secrets
import time

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from dashboard.config import DASHBOARD_USERNAME, DASHBOARD_PASSWORD

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
    """Check username and password."""
    return username == DASHBOARD_USERNAME and password == DASHBOARD_PASSWORD


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
