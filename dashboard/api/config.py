"""Configuration API — safe .env editing + LLM settings."""

import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dashboard.auth import verify_token
from kronos.config import settings

router = APIRouter(prefix="/api/config", tags=["config"], dependencies=[Depends(verify_token)])
log = logging.getLogger("kronos.dashboard.config")

# Keys that should never be exposed in full
SECRET_PATTERNS = re.compile(r"(KEY|SECRET|TOKEN|HASH|PASSWORD|COOKIE)", re.I)


def _mask_value(key: str, value: str) -> str:
    if SECRET_PATTERNS.search(key) and len(value) > 8:
        return value[:4] + "*" * (len(value) - 8) + value[-4:]
    return value


def _get_env_path() -> Path:
    return Path(settings.db_path).parent.parent / "app" / ".env"


@router.get("/env")
async def get_env_vars():
    """Get .env variables with masked secrets."""
    env_path = _get_env_path()
    if not env_path.exists():
        return {"vars": []}

    result = []
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        result.append({
            "key": key,
            "value": _mask_value(key, value),
            "is_secret": bool(SECRET_PATTERNS.search(key)),
        })
    return {"vars": result}


class EnvUpdate(BaseModel):
    value: str


@router.put("/env/{key}")
async def update_env_var(key: str, body: EnvUpdate):
    """Update a single .env variable."""
    env_path = _get_env_path()
    if not env_path.exists():
        raise HTTPException(404, ".env file not found")

    lines = env_path.read_text().splitlines()
    found = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, _ = stripped.partition("=")
            if k.strip() == key:
                new_lines.append(f"{key}={body.value}")
                found = True
                continue
        new_lines.append(line)

    if not found:
        new_lines.append(f"{key}={body.value}")

    env_path.write_text("\n".join(new_lines) + "\n")
    log.info("Env var updated: %s", key)
    return {"ok": True, "key": key, "note": "Restart required for changes to take effect"}


@router.get("/llm")
async def get_llm_config():
    """Get current LLM configuration."""
    from kronos.llm import ModelTier, get_model
    from kronos.router import COMPLEX_PATTERNS, SIMPLE_PATTERNS_RU

    models = {}
    for tier in ModelTier:
        try:
            m = get_model(tier)
            models[tier.value] = {
                "model": getattr(m, "model_name", getattr(m, "model", "unknown")),
                "temperature": getattr(m, "temperature", None),
                "max_tokens": getattr(m, "max_tokens", None),
            }
        except Exception:
            models[tier.value] = {"model": "not configured"}

    return {
        "tiers": models,
        "routing": {
            "complex_patterns_count": len(COMPLEX_PATTERNS),
            "simple_patterns_count": len(SIMPLE_PATTERNS_RU),
        },
    }
