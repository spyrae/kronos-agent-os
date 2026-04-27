"""Health, status, and auth endpoints."""

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from dashboard.auth import create_session, verify_credentials
from kronos.config import settings

router = APIRouter(tags=["health"])

_start_time = time.time()


class LoginRequest(BaseModel):
    username: str
    password: str


@router.get("/api/health")
async def health():
    return {
        "status": "ok",
        "agent": settings.agent_name,
        "uptime_seconds": int(time.time() - _start_time),
    }


@router.post("/api/auth/login")
async def login(body: LoginRequest):
    """Authenticate with username/password, returns session token."""
    if not verify_credentials(body.username, body.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_session()
    return {"token": token}
