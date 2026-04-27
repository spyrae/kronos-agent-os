"""Configuration API — safe .env editing + LLM settings."""

from datetime import UTC, datetime
import json
import logging
import os
import re
from pathlib import Path
from uuid import uuid4

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
    explicit = os.environ.get("KAOS_ENV_FILE") or os.environ.get("KRONOS_ENV_FILE")
    if explicit:
        return Path(explicit).expanduser()

    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        return cwd_env

    project_env = Path(__file__).resolve().parents[2] / ".env"
    if project_env.exists():
        return project_env

    return cwd_env


def _approval_events_path() -> Path:
    return Path(settings.db_path).parent / "logs" / "approval_queue.jsonl"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _append_approval_event(event: dict) -> None:
    path = _approval_events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


def _load_approvals() -> list[dict]:
    path = _approval_events_path()
    if not path.exists():
        return []

    approvals: dict[str, dict] = {}
    with open(path) as f:
        for line in f:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            approval_id = event.get("id")
            if not approval_id:
                continue
            if event.get("event") == "created":
                approvals[approval_id] = {k: v for k, v in event.items() if k != "event"}
            elif event.get("event") == "decided" and approval_id in approvals:
                approvals[approval_id].update({
                    "status": event.get("decision", approvals[approval_id].get("status")),
                    "decision_reason": event.get("reason", ""),
                    "decided_at": event.get("decided_at", ""),
                    "decided_by": event.get("decided_by", ""),
                })

    return sorted(approvals.values(), key=lambda item: item.get("requested_at", ""), reverse=True)


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


class ApprovalCreate(BaseModel):
    capability: str
    action: str
    reason: str = ""


class ApprovalDecision(BaseModel):
    decision: str
    reason: str = ""


def _capability(
    key: str,
    name: str,
    enabled: bool,
    risk: str,
    description: str,
    required_env: str,
) -> dict:
    workspace = settings.workspace_path or f"workspaces/{settings.agent_name}"
    return {
        "key": key,
        "name": name,
        "enabled": enabled,
        "status": "enabled" if enabled else "blocked",
        "risk": risk,
        "description": description,
        "required_env": required_env,
        "scope": "runtime",
        "owner": workspace,
        "change_mode": "approval_required_restart",
        "can_request_change": True,
    }


def _build_capabilities() -> list[dict]:
    return [
        _capability(
            key="dynamic_tools",
            name="Dynamic tools",
            enabled=settings.enable_dynamic_tools,
            risk="high",
            description="Allows runtime-created Python tools.",
            required_env="ENABLE_DYNAMIC_TOOLS=true",
        ),
        _capability(
            key="dynamic_tool_sandbox",
            name="Dynamic tool sandbox requirement",
            enabled=settings.require_dynamic_tool_sandbox,
            risk="protective",
            description="Requires sandboxed execution for dynamic tools.",
            required_env="REQUIRE_DYNAMIC_TOOL_SANDBOX=true",
        ),
        _capability(
            key="mcp_gateway_management",
            name="MCP gateway management",
            enabled=settings.enable_mcp_gateway_management,
            risk="high",
            description="Allows adding, removing, and reloading MCP servers at runtime.",
            required_env="ENABLE_MCP_GATEWAY_MANAGEMENT=true",
        ),
        _capability(
            key="dynamic_mcp_servers",
            name="Persisted dynamic MCP servers",
            enabled=settings.enable_dynamic_mcp_servers,
            risk="high",
            description="Loads MCP servers that were registered dynamically.",
            required_env="ENABLE_DYNAMIC_MCP_SERVERS=true",
        ),
        _capability(
            key="server_ops",
            name="Server operations",
            enabled=settings.enable_server_ops,
            risk="critical",
            description="Enables SSH/systemd/docker diagnostics and whitelisted server actions.",
            required_env="ENABLE_SERVER_OPS=true plus private servers.yaml",
        ),
    ]


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


@router.get("/capabilities")
async def get_capabilities():
    """Get public-safe capability gate status."""
    return {"capabilities": _build_capabilities()}


@router.get("/approvals")
async def get_approvals():
    """Get capability approval queue."""
    approvals = _load_approvals()
    return {
        "approvals": approvals,
        "pending": sum(1 for item in approvals if item.get("status") == "pending"),
        "recent": [item for item in approvals if item.get("status") != "pending"][:20],
    }


@router.post("/approvals")
async def create_approval(body: ApprovalCreate):
    """Stage a capability change for human approval."""
    capabilities = {item["key"]: item for item in _build_capabilities()}
    capability = capabilities.get(body.capability)
    if capability is None:
        raise HTTPException(404, f"Unknown capability: {body.capability}")
    if body.action not in {"enable", "disable"}:
        raise HTTPException(400, "action must be enable or disable")

    for existing in _load_approvals():
        if (
            existing.get("status") == "pending"
            and existing.get("capability") == body.capability
            and existing.get("action") == body.action
        ):
            return {"ok": True, "approval": existing, "deduplicated": True}

    approval = {
        "id": f"apr_{uuid4().hex[:12]}",
        "event": "created",
        "kind": "capability_change",
        "capability": body.capability,
        "capability_name": capability["name"],
        "action": body.action,
        "status": "pending",
        "risk": capability["risk"],
        "scope": capability["scope"],
        "owner": capability["owner"],
        "required_env": capability["required_env"],
        "reason": body.reason,
        "requested_at": _now_iso(),
        "requested_by": "dashboard",
        "effect": "no_runtime_change_until_env_restart",
    }
    _append_approval_event(approval)
    log.info("Approval requested: %s %s", body.action, body.capability)
    return {"ok": True, "approval": {k: v for k, v in approval.items() if k != "event"}}


@router.post("/approvals/{approval_id}/decision")
async def decide_approval(approval_id: str, body: ApprovalDecision):
    """Approve or deny a pending capability request."""
    if body.decision not in {"approved", "denied"}:
        raise HTTPException(400, "decision must be approved or denied")

    approvals = {item["id"]: item for item in _load_approvals()}
    approval = approvals.get(approval_id)
    if approval is None:
        raise HTTPException(404, f"Approval not found: {approval_id}")
    if approval.get("status") != "pending":
        raise HTTPException(409, f"Approval is already {approval.get('status')}")

    event = {
        "event": "decided",
        "id": approval_id,
        "decision": body.decision,
        "reason": body.reason,
        "decided_at": _now_iso(),
        "decided_by": "dashboard",
    }
    _append_approval_event(event)
    approval.update({
        "status": body.decision,
        "decision_reason": body.reason,
        "decided_at": event["decided_at"],
        "decided_by": "dashboard",
    })
    log.info("Approval %s: %s", approval_id, body.decision)
    return {"ok": True, "approval": approval}


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
