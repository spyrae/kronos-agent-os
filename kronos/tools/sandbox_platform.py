"""Sandbox platform policy, audit, and workspace primitives.

This module is intentionally execution-engine agnostic. The current dynamic
tool runner still uses ``kronos.tools.sandbox`` for the basic Docker image;
the platform layer adds product-grade controls around future runs:

* per-session workspace directories,
* default-deny network and package policy,
* secret capability declarations instead of raw secret injection,
* resource budget declarations,
* durable audit records for dashboard visibility.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kronos.config import settings
from kronos.security.pii import mask_pii_object

SECRET_PLACEHOLDER = "[REDACTED]"
SANDBOX_LOG_NAME = "sandbox_runs.jsonl"
_SECRET_VALUE_RE = re.compile(r"\b(?:sk|ghp|xox[baprs]?)-[A-Za-z0-9_\-]{8,}\b")


@dataclass(frozen=True)
class SandboxResourceLimits:
    """Resource budget for one sandbox run."""

    cpu: float = 1.0
    memory_mb: int = 256
    timeout_seconds: int = 30
    process_count: int = 50
    storage_mb: int = 64


@dataclass(frozen=True)
class SandboxPolicy:
    """Allowlist policy for a sandbox run."""

    allowed_inputs: tuple[str, ...] = ()
    allowed_network_domains: tuple[str, ...] = ()
    allowed_packages: tuple[str, ...] = ()
    allowed_secret_capabilities: tuple[str, ...] = ()
    max_resources: SandboxResourceLimits = field(default_factory=SandboxResourceLimits)


@dataclass(frozen=True)
class SandboxRunRequest:
    """Declared sandbox run requirements."""

    tool_name: str
    session_id: str
    input_mounts: tuple[str, ...] = ()
    output_mounts: tuple[str, ...] = ("artifacts",)
    network_domains: tuple[str, ...] = ()
    packages: tuple[str, ...] = ()
    secret_capabilities: tuple[str, ...] = ()
    resources: SandboxResourceLimits = field(default_factory=SandboxResourceLimits)


@dataclass(frozen=True)
class PolicyDecision:
    """Result of evaluating a run request against a policy."""

    allowed: bool
    reason: str
    violations: tuple[str, ...] = ()


def evaluate_policy(request: SandboxRunRequest, policy: SandboxPolicy | None = None) -> PolicyDecision:
    """Evaluate a run request using fail-closed allowlists."""
    policy = policy or SandboxPolicy()
    violations: list[str] = []

    allowed_inputs = set(policy.allowed_inputs)
    if allowed_inputs and "*" not in allowed_inputs:
        for name in request.input_mounts:
            if name not in allowed_inputs:
                violations.append(f"input:{name}")

    allowed_domains = tuple(_normalize_domain(domain) for domain in policy.allowed_network_domains)
    for domain in request.network_domains:
        normalized = _normalize_domain(domain)
        if not normalized or not _domain_allowed(normalized, allowed_domains):
            violations.append(f"network:{normalized or domain}")

    allowed_packages = {_normalize_package(pkg) for pkg in policy.allowed_packages}
    for package in request.packages:
        normalized = _normalize_package(package)
        if not normalized or normalized not in allowed_packages:
            violations.append(f"package:{normalized or package}")

    allowed_secrets = set(policy.allowed_secret_capabilities)
    for capability in request.secret_capabilities:
        if capability not in allowed_secrets:
            violations.append(f"secret:{capability}")

    violations.extend(_resource_violations(request.resources, policy.max_resources))

    if violations:
        return PolicyDecision(False, "blocked by sandbox policy", tuple(violations))
    return PolicyDecision(True, "allowed by sandbox policy")


def create_session_workspace(
    request: SandboxRunRequest,
    *,
    base_dir: Path | None = None,
) -> dict[str, str]:
    """Create an isolated workspace skeleton for a sandbox session."""
    root = (base_dir or sandbox_workspace_root()) / _safe_path_part(request.session_id) / _run_id(request)
    paths = {
        "root": root,
        "inputs": root / "inputs",
        "outputs": root / "outputs",
        "artifacts": root / "artifacts",
        "tmp": root / "tmp",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)

    manifest = {
        "run_id": root.name,
        "session_id": request.session_id,
        "tool_name": request.tool_name,
        "input_mounts": list(request.input_mounts),
        "output_mounts": list(request.output_mounts),
        "created_at": _now(),
    }
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {key: str(path) for key, path in paths.items()}


def record_sandbox_decision(
    request: SandboxRunRequest,
    decision: PolicyDecision,
    policy: SandboxPolicy | None = None,
    *,
    stdout: str = "",
    stderr: str = "",
    artifacts: list[dict[str, Any]] | None = None,
    resources_used: dict[str, Any] | None = None,
    audit_path: Path | None = None,
) -> dict[str, Any]:
    """Write a durable redacted sandbox audit record."""
    policy = policy or SandboxPolicy()
    record = {
        "run_id": _run_id(request),
        "ts": _now(),
        "status": "allowed" if decision.allowed else "blocked",
        "tool_name": request.tool_name,
        "session_id": request.session_id,
        "request": _dataclass_payload(request),
        "policy": _dataclass_payload(policy),
        "decision": _dataclass_payload(decision),
        "resources_used": resources_used or {},
        "stdout_summary": stdout[:1000],
        "stderr_summary": stderr[:1000],
        "artifacts": artifacts or [],
    }
    write_sandbox_record(record, audit_path=audit_path)
    return _redact(record)


def write_sandbox_record(record: dict[str, Any], *, audit_path: Path | None = None) -> None:
    """Append a redacted sandbox audit record to JSONL."""
    path = audit_path or sandbox_audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_record = _redact(record)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe_record, ensure_ascii=False, sort_keys=True) + "\n")


def read_sandbox_records(
    *,
    limit: int = 100,
    status: str = "all",
    audit_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Read recent sandbox audit records newest-first."""
    path = audit_path or sandbox_audit_path()
    if not path.exists():
        return []

    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if status != "all" and row.get("status") != status:
            continue
        rows.append(row)
    return list(reversed(rows[-limit:]))


def sandbox_platform_status() -> dict[str, Any]:
    """Return readiness for the platform layer separately from Docker image readiness."""
    from kronos.tools.sandbox import sandbox_status

    basic = sandbox_status()
    audit_path = sandbox_audit_path()
    workspace_root = sandbox_workspace_root()
    return {
        "basic_sandbox": basic,
        "platform": {
            "ready": True,
            "execution_ready": bool(basic.get("docker_available") and basic.get("image_available")),
            "network_default": "deny",
            "secret_proxy": "capability-declared",
            "package_policy": "allowlist",
            "resource_accounting": True,
            "audit_log": str(audit_path),
            "workspace_root": str(workspace_root),
        },
    }


def sandbox_audit_path() -> Path:
    """Return the sandbox audit JSONL path."""
    return Path(settings.db_path).parent / "logs" / SANDBOX_LOG_NAME


def sandbox_workspace_root() -> Path:
    """Return the base directory for isolated sandbox workspaces."""
    return Path(settings.db_path).parent / "sandbox"


def _resource_violations(resources: SandboxResourceLimits, maximum: SandboxResourceLimits) -> list[str]:
    violations: list[str] = []
    if resources.cpu > maximum.cpu:
        violations.append(f"resource:cpu>{maximum.cpu:g}")
    if resources.memory_mb > maximum.memory_mb:
        violations.append(f"resource:memory>{maximum.memory_mb}mb")
    if resources.timeout_seconds > maximum.timeout_seconds:
        violations.append(f"resource:timeout>{maximum.timeout_seconds}s")
    if resources.process_count > maximum.process_count:
        violations.append(f"resource:processes>{maximum.process_count}")
    if resources.storage_mb > maximum.storage_mb:
        violations.append(f"resource:storage>{maximum.storage_mb}mb")
    return violations


def _normalize_domain(value: str) -> str:
    domain = value.strip().lower()
    domain = re.sub(r"^https?://", "", domain)
    domain = domain.split("/", 1)[0].split(":", 1)[0]
    return domain.strip(".")


def _domain_allowed(domain: str, allowlist: tuple[str, ...]) -> bool:
    for allowed in allowlist:
        if not allowed:
            continue
        if allowed.startswith("*."):
            base = allowed[2:]
            if domain == base or domain.endswith(f".{base}"):
                return True
        if domain == allowed:
            return True
    return False


def _normalize_package(value: str) -> str:
    name = re.split(r"[<>=!~\[]", value.strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


def _dataclass_payload(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return value


def _redact(value: Any) -> Any:
    masked = mask_pii_object(value)
    return _redact_secrets(masked)


def _redact_secrets(value: Any, *, parent_key: str = "") -> Any:
    if isinstance(value, dict):
        return {key: _redact_secrets(item, parent_key=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secrets(item, parent_key=parent_key) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_secrets(item, parent_key=parent_key) for item in value)
    if isinstance(value, str):
        if _looks_secret(parent_key):
            return SECRET_PLACEHOLDER
        return _SECRET_VALUE_RE.sub(SECRET_PLACEHOLDER, value)
    return value


def _looks_secret(key: str) -> bool:
    normalized = key.lower()
    if normalized in {"secret_capabilities", "allowed_secret_capabilities"}:
        return False
    return any(marker in normalized for marker in ("secret", "token", "api_key", "apikey", "password", "credential"))


def _safe_path_part(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-")[:80] or "session"


def _run_id(request: SandboxRunRequest) -> str:
    raw = json.dumps(_dataclass_payload(request), sort_keys=True, default=str)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"sbox_{digest}"


def _now() -> str:
    return datetime.now(UTC).isoformat()
