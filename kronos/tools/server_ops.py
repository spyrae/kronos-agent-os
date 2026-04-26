"""Server operations tools — SSH-based diagnostics and management.

Level 1 (read-only): logs, status, health checks, DB queries.
Level 2 (actions): restart services, deploy, clear cache — whitelist only.

All commands go through asyncssh with key-based auth.
Arbitrary shell access is NEVER allowed.
"""

import asyncio
import logging
import os
import re

import asyncssh
import yaml
from langchain_core.tools import tool

log = logging.getLogger("kronos.tools.server_ops")

# ── Server registry ──────────────────────────────────────────────────────
# Loaded from servers.yaml (gitignored). See servers.example.yaml for format.

def _load_registry() -> dict[str, dict]:
    """Load server registry from YAML config."""
    config_path = os.environ.get(
        "SERVER_REGISTRY_PATH",
        os.path.join(os.path.dirname(__file__), "..", "..", "servers.yaml"),
    )
    config_path = os.path.normpath(config_path)
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    log.warning("Server registry not found at %s — server_ops tools disabled", config_path)
    return {}

SERVER_REGISTRY: dict[str, dict] = _load_registry()

_EXAMPLE_REGISTRY: dict[str, dict] = {
    # See servers.example.yaml for full format
    # Example: see servers.example.yaml for full format
    "my-vps": {
        "host": "1.2.3.4",
        "username": "deploy",
        "app_path": "/opt/kronos-swarm/app",
        "data_path": "/opt/kronos-swarm/app/data",
        "description": "Main VPS — Kronos Swarm agents",
        "services": ["kronos", "nexus"],
        "projects": {
            "kronos-swarm": {
                "path": "/opt/kronos-swarm/app",
                "services": ["kronos", "nexus"],
                "description": "AI agent swarm",
            },
        },
    },
}

# ── Whitelisted commands (Level 2) ───────────────────────────────────────
# Collect from registry: all services mentioned in any server entry.

def _collect_services() -> set[str]:
    services = set()
    for srv in SERVER_REGISTRY.values():
        services.update(srv.get("services", []))
        for proj in srv.get("projects", {}).values():
            services.update(proj.get("services", []))
    return services

ALLOWED_RESTART_SERVICES = _collect_services()

# ── Server discovery ─────────────────────────────────────────────────────


@tool
def server_list() -> str:
    """List all known servers, projects, and services.

    Use this first to understand which server hosts the project/service
    that the user is asking about.
    """
    lines = ["=== Server Registry ===\n"]
    for name, srv in SERVER_REGISTRY.items():
        lines.append(f"## {name}")
        lines.append(f"  Host: {srv.get('host', 'API-only (no SSH)')}")
        lines.append(f"  Description: {srv['description']}")
        if "projects" in srv:
            for proj_name, proj in srv["projects"].items():
                lines.append(f"  Project: {proj_name}")
                lines.append(f"    {proj['description']}")
                if "services" in proj:
                    lines.append(f"    Services: {', '.join(proj['services'])}")
                if "manage_via" in proj:
                    lines.append(f"    Manage via: {proj['manage_via']}")
                if "path" in proj:
                    lines.append(f"    Path: {proj['path']}")
        if "services" in srv:
            lines.append(f"  All services: {', '.join(srv['services'])}")
        lines.append("")
    return "\n".join(lines)


# ── SSH connection helper ────────────────────────────────────────────────

_SSH_TIMEOUT = 30  # seconds


async def _ssh_run(
    host: str,
    command: str,
    username: str = "roman",
    timeout: int = _SSH_TIMEOUT,
) -> str:
    """Execute a command over SSH and return stdout.

    Uses key-based auth from the agent's SSH key (~/.ssh/id_ed25519 or id_rsa).
    """
    try:
        async with asyncssh.connect(
            host,
            username=username,
            known_hosts=None,  # server key already trusted
            connect_timeout=timeout,
        ) as conn:
            result = await asyncio.wait_for(
                conn.run(command, check=False),
                timeout=timeout,
            )
            output = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()

            if result.exit_status != 0:
                return f"[exit={result.exit_status}]\n{output}\n{stderr}".strip()
            return output or "(no output)"

    except asyncssh.DisconnectError as e:
        log.error("SSH disconnect to %s: %s", host, e)
        return f"[SSH ERROR] Connection lost: {e}"
    except TimeoutError:
        log.error("SSH timeout to %s after %ds", host, timeout)
        return f"[SSH ERROR] Timeout after {timeout}s"
    except Exception as e:
        log.error("SSH error to %s: %s", host, e)
        return f"[SSH ERROR] {type(e).__name__}: {e}"


def _get_server(server_name: str) -> dict | None:
    return SERVER_REGISTRY.get(server_name)


# ── Level 1: Read-only tools ────────────────────────────────────────────


@tool
async def server_status(server_name: str = "fra-01") -> str:
    """Get server overview: uptime, load, memory, disk usage.

    Args:
        server_name: Server name from registry (default: kronos).
    """
    srv = _get_server(server_name)
    if not srv:
        return f"Unknown server: {server_name}. Available: {list(SERVER_REGISTRY)}"

    cmd = (
        "echo '=== UPTIME ===' && uptime && "
        "echo '\\n=== MEMORY ===' && free -h && "
        "echo '\\n=== DISK ===' && df -h / && "
        "echo '\\n=== LOAD ===' && cat /proc/loadavg"
    )
    return await _ssh_run(srv["host"], cmd, srv["username"])


@tool
async def server_service_status(
    service_name: str,
    server_name: str = "fra-01",
) -> str:
    """Check systemd service status (active/failed, uptime, recent logs).

    Args:
        service_name: Service name (e.g. 'kronos-ii', 'impulse', 'nexus').
        server_name: Server from registry (default: kronos).
    """
    srv = _get_server(server_name)
    if not srv:
        return f"Unknown server: {server_name}"

    if service_name not in srv["services"]:
        return f"Unknown service: {service_name}. Available: {srv['services']}"

    cmd = f"systemctl status {service_name} --no-pager -l 2>&1 | head -25"
    return await _ssh_run(srv["host"], cmd, srv["username"])


@tool
async def server_all_services(server_name: str = "fra-01") -> str:
    """Quick status of ALL services on the server (one-line per service).

    Args:
        server_name: Server from registry (default: kronos).
    """
    srv = _get_server(server_name)
    if not srv:
        return f"Unknown server: {server_name}"

    checks = " && ".join(
        f"echo '{svc}: '$(systemctl is-active {svc})' | '$(systemctl show {svc} --property=ActiveEnterTimestamp --value)"
        for svc in srv["services"]
    )
    return await _ssh_run(srv["host"], checks, srv["username"])


@tool
async def server_logs(
    service_name: str,
    lines: int = 50,
    grep_pattern: str = "",
    server_name: str = "fra-01",
) -> str:
    """View recent logs for a systemd service via journalctl.

    Args:
        service_name: Service name (e.g. 'impulse').
        lines: Number of lines to fetch (default 50, max 200).
        grep_pattern: Optional grep filter (e.g. 'ERROR', 'timeout').
        server_name: Server from registry (default: kronos).
    """
    srv = _get_server(server_name)
    if not srv:
        return f"Unknown server: {server_name}"

    if service_name not in srv["services"]:
        return f"Unknown service: {service_name}. Available: {srv['services']}"

    lines = min(max(lines, 10), 200)
    cmd = f"journalctl -u {service_name} --no-pager -n {lines} --output=short-iso"

    if grep_pattern:
        # Sanitize grep pattern — only allow safe chars
        safe_pattern = re.sub(r'[^a-zA-Z0-9_\-.\s|]', '', grep_pattern)
        if safe_pattern:
            cmd += f" | grep -i '{safe_pattern}'"

    return await _ssh_run(srv["host"], cmd, srv["username"])


@tool
async def server_errors(
    service_name: str = "",
    minutes: int = 30,
    server_name: str = "fra-01",
) -> str:
    """Find recent errors across one or all services.

    Args:
        service_name: Specific service, or empty for all agent services.
        minutes: Look back N minutes (default 30, max 1440).
        server_name: Server from registry (default: kronos).
    """
    srv = _get_server(server_name)
    if not srv:
        return f"Unknown server: {server_name}"

    minutes = min(max(minutes, 5), 1440)

    if service_name:
        if service_name not in srv["services"]:
            return f"Unknown service: {service_name}"
        unit_filter = f"-u {service_name}"
    else:
        unit_filter = " ".join(f"-u {s}" for s in srv["services"])

    cmd = (
        f"journalctl {unit_filter} --no-pager --since '{minutes} min ago' "
        f"-p err --output=short-iso | tail -100"
    )
    return await _ssh_run(srv["host"], cmd, srv["username"])


@tool
async def server_query_swarm(
    query: str,
    server_name: str = "fra-01",
) -> str:
    """Run a READ-ONLY SQL query on swarm.db (shared cross-agent ledger).

    Only SELECT queries are allowed. Useful for checking:
    - reply_claims: who replied to what, arbitration results
    - swarm_messages: recent message flow
    - swarm_metrics: addressing stats, duplicate prevention
    - shared_user_facts: cross-agent user profile

    Args:
        query: SQL SELECT query.
        server_name: Server from registry (default: kronos).
    """
    srv = _get_server(server_name)
    if not srv:
        return f"Unknown server: {server_name}"

    # Strict read-only check
    normalized = query.strip().upper()
    if not normalized.startswith("SELECT"):
        return "[BLOCKED] Only SELECT queries are allowed on swarm.db."

    # Block dangerous keywords even in SELECT
    dangerous = ["DROP", "DELETE", "INSERT", "UPDATE", "ALTER", "CREATE", "ATTACH", "DETACH"]
    for keyword in dangerous:
        if keyword in normalized:
            return f"[BLOCKED] Query contains forbidden keyword: {keyword}"

    db_path = f"{srv['data_path']}/swarm.db"
    cmd = f'sqlite3 -header -column "{db_path}" "{query}" 2>&1 | head -100'
    return await _ssh_run(srv["host"], cmd, srv["username"])


@tool
async def server_disk_detail(server_name: str = "fra-01") -> str:
    """Detailed disk usage: largest dirs, SQLite DB sizes, /tmp, log size.

    Args:
        server_name: Server from registry (default: kronos).
    """
    srv = _get_server(server_name)
    if not srv:
        return f"Unknown server: {server_name}"

    cmd = (
        f"echo '=== APP DATA ===' && du -sh {srv['data_path']}/*/ 2>/dev/null && "
        f"echo '\\n=== DB FILES ===' && find {srv['data_path']} -name '*.db' -exec ls -lh {{}} \\; && "
        f"echo '\\n=== JOURNAL ===' && journalctl --disk-usage && "
        f"echo '\\n=== /tmp ===' && du -sh /tmp 2>/dev/null"
    )
    return await _ssh_run(srv["host"], cmd, srv["username"])


# ── Level 2: Whitelisted actions ─────────────────────────────────────────


@tool
async def server_restart_service(
    service_name: str,
    server_name: str = "fra-01",
) -> str:
    """Restart a systemd service. Only whitelisted services allowed.

    ⚠️ This is a Level 2 action — it modifies server state.

    Args:
        service_name: Service to restart (must be in whitelist).
        server_name: Server from registry (default: kronos).
    """
    srv = _get_server(server_name)
    if not srv:
        return f"Unknown server: {server_name}"

    if service_name not in ALLOWED_RESTART_SERVICES:
        return (
            f"[BLOCKED] Service '{service_name}' is not in restart whitelist. "
            f"Allowed: {sorted(ALLOWED_RESTART_SERVICES)}"
        )

    cmd = (
        f"sudo systemctl restart {service_name} && "
        f"sleep 2 && "
        f"systemctl is-active {service_name}"
    )
    result = await _ssh_run(srv["host"], cmd, srv["username"])
    log.info("Restarted service %s on %s: %s", service_name, server_name, result)
    return f"Restart {service_name}: {result}"


@tool
async def server_clear_journal(
    older_than: str = "3d",
    server_name: str = "fra-01",
) -> str:
    """Clear old systemd journal logs to free disk space.

    Args:
        older_than: Keep logs newer than this (e.g. '3d', '1w'). Default: 3d.
        server_name: Server from registry (default: kronos).
    """
    srv = _get_server(server_name)
    if not srv:
        return f"Unknown server: {server_name}"

    # Validate time format
    if not re.match(r'^\d+[dhwm]$', older_than):
        return "[BLOCKED] Invalid time format. Use e.g. '3d', '1w', '12h'."

    cmd = (
        f"sudo journalctl --vacuum-time={older_than} 2>&1 && "
        f"echo '\\n=== After cleanup ===' && journalctl --disk-usage"
    )
    return await _ssh_run(srv["host"], cmd, srv["username"])


# ── Docker tools ─────────────────────────────────────────────────────────


@tool
async def docker_ps(server_name: str = "fra-01") -> str:
    """List running Docker containers on a server.

    Args:
        server_name: Server from registry.
    """
    srv = _get_server(server_name)
    if not srv or not srv.get("host"):
        return f"Unknown or non-SSH server: {server_name}"

    cmd = "docker ps --format 'table {{.Names}}\\t{{.Status}}\\t{{.Image}}' 2>/dev/null || sudo docker ps --format 'table {{.Names}}\\t{{.Status}}\\t{{.Image}}'"
    return await _ssh_run(srv["host"], cmd, srv["username"])


@tool
async def docker_logs(
    container_name: str,
    lines: int = 50,
    server_name: str = "fra-01",
) -> str:
    """View recent logs for a Docker container.

    Args:
        container_name: Container name (e.g. 'langfuse-langfuse-web-1', 'grafana').
        lines: Number of lines (default 50, max 200).
        server_name: Server from registry.
    """
    srv = _get_server(server_name)
    if not srv or not srv.get("host"):
        return f"Unknown or non-SSH server: {server_name}"

    # Validate container name — alphanumeric, hyphens, underscores only
    if not re.match(r'^[a-zA-Z0-9_\-]+$', container_name):
        return "[BLOCKED] Invalid container name."

    lines = min(max(lines, 10), 200)
    cmd = f"docker logs {container_name} --tail {lines} 2>&1 || sudo docker logs {container_name} --tail {lines} 2>&1"
    return await _ssh_run(srv["host"], cmd, srv["username"])


@tool
async def docker_restart(
    container_name: str,
    server_name: str = "fra-01",
) -> str:
    """Restart a Docker container.

    ⚠️ Level 2 action — modifies server state.

    Args:
        container_name: Container to restart.
        server_name: Server from registry.
    """
    srv = _get_server(server_name)
    if not srv or not srv.get("host"):
        return f"Unknown or non-SSH server: {server_name}"

    if not re.match(r'^[a-zA-Z0-9_\-]+$', container_name):
        return "[BLOCKED] Invalid container name."

    # Verify container exists in known docker list for this server
    known_containers = srv.get("docker", [])
    # Also check project-level docker lists
    for proj in srv.get("projects", {}).values():
        known_containers.extend(proj.get("docker", []))

    if container_name not in known_containers:
        return (
            f"[BLOCKED] Container '{container_name}' not in known list for {server_name}. "
            f"Known: {sorted(set(known_containers))}"
        )

    cmd = (
        f"docker restart {container_name} 2>/dev/null || sudo docker restart {container_name} && "
        f"sleep 3 && "
        f"docker ps --filter name={container_name} --format '{{{{.Names}}}}: {{{{.Status}}}}' 2>/dev/null || "
        f"sudo docker ps --filter name={container_name} --format '{{{{.Names}}}}: {{{{.Status}}}}'"
    )
    result = await _ssh_run(srv["host"], cmd, srv["username"])
    log.info("Restarted container %s on %s: %s", container_name, server_name, result)
    return f"Restart {container_name}: {result}"


# ── Tool collection ──────────────────────────────────────────────────────


def get_server_ops_tools() -> list:
    """Get all server operations tools.

    Returns Level 1 (read-only) and Level 2 (whitelist actions) tools.
    """
    return [
        # Discovery
        server_list,
        # Level 1 — read-only
        server_status,
        server_service_status,
        server_all_services,
        server_logs,
        server_errors,
        server_query_swarm,
        server_disk_detail,
        # Docker
        docker_ps,
        docker_logs,
        # Level 2 — whitelist actions
        server_restart_service,
        server_clear_journal,
        docker_restart,
    ]
