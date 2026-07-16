"""Health, status, and auth endpoints."""

import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from dashboard.auth import (
    clear_login_failures,
    clear_session_cookie,
    create_session,
    invalidate_session,
    login_retry_after,
    record_login_failure,
    set_session_cookie,
    verify_credentials,
    verify_token,
)
from kronos.config import settings

router = APIRouter(tags=["health"])


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _cookie_secure(request: Request) -> bool:
    """Set the Secure flag when the request arrived over TLS (directly or via
    a TLS-terminating proxy that forwards X-Forwarded-Proto)."""
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"


_start_time = time.time()


class LoginRequest(BaseModel):
    username: str
    password: str


@router.get("/api/health")
async def health():
    from kronos import __version__

    return {
        "status": "ok",
        "agent": settings.agent_name,
        "version": __version__,
        "uptime_seconds": int(time.time() - _start_time),
    }


@router.post("/api/auth/login")
async def login(body: LoginRequest, request: Request, response: Response):
    """Authenticate with username/password; set the session cookie.

    Rate-limited per client IP. The token is delivered only as an HttpOnly
    cookie — it is deliberately not returned in the body, so it never reaches
    JavaScript or a browser's storage.
    """
    ip = _client_ip(request)
    retry_after = login_retry_after(ip)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    if not verify_credentials(body.username, body.password):
        record_login_failure(ip)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    clear_login_failures(ip)
    token = create_session()
    set_session_cookie(response, token, secure=_cookie_secure(request))
    return {"ok": True}


@router.post("/api/auth/logout")
async def logout(response: Response, token: str = Depends(verify_token)):
    """Invalidate the current session and clear the cookie."""
    invalidate_session(token)
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/api/auth/me")
async def me(token: str = Depends(verify_token)):
    """Lightweight auth probe — 200 when the session cookie is valid, else 401.

    The SPA calls this on load to decide whether to show the app or the login
    screen, since it can no longer read the HttpOnly cookie itself.
    """
    return {"authenticated": True}
