"""Audit and cost logging — tracks every request with approximate cost.

Logs to JSONL files:
- audit.jsonl: full request/response audit trail
- cost.jsonl: cost tracking per request
- tool_calls.jsonl: durable tool-call trail
"""

import hashlib
import json
import logging
import math
import re
import threading
import time
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

from kronos.config import settings
from kronos.security.pii import mask_pii

log = logging.getLogger("kronos.audit")

# DeepSeek V3 pricing (per 1M tokens)
COST_TABLE = {
    "lite": {"input": 0.27, "output": 1.10},
    "standard": {"input": 0.27, "output": 1.10},  # same model for now
    "blocked": {"input": 0, "output": 0},
}

_audit_dir: Path | None = None
_tool_audit_context: ContextVar[dict[str, str]] = ContextVar("tool_audit_context", default={})
# Field names whose values are secrets regardless of content. Public because
# every path that persists agent data (audit log, exported bundles) must redact
# the same set — a second copy of this list would drift.
SECRET_FIELD_NAMES = frozenset({"token", "secret", "password", "api_key", "apikey", "key", "hash", "authorization"})
_SECRET_PATTERNS = (
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{12,}"), "Bearer ***REDACTED***"),
    (re.compile(r"sk-[A-Za-z0-9_-]{12,}"), "sk-***REDACTED***"),
    (re.compile(r"(api[_-]?key=)[^&\s]+", re.IGNORECASE), r"\1***REDACTED***"),
    (re.compile(r"(token=)[^&\s]+", re.IGNORECASE), r"\1***REDACTED***"),
)


# --- tamper-evident chaining -------------------------------------------------
#
# Each entry carries prev_hash (the previous entry's hash) and entry_hash, so a
# deleted or edited line breaks the chain and `kaos audit verify` says where.
# This does not prevent tampering — a local attacker can rewrite the whole file —
# it makes silent tampering detectable, which is what an audit trail is for.
GENESIS_HASH = "genesis"
CHAINED_LOGS = ("tool_calls.jsonl", "audit.jsonl", "credentials.jsonl")

_chain_tips: dict[str, str] = {}
_chain_lock = threading.Lock()


def entry_hash(entry: dict) -> str:
    """Hash of one entry, excluding its own hash field."""
    payload = {key: value for key, value in entry.items() if key != "entry_hash"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _last_hash_in_file(path: Path) -> str:
    """Read the tip of an existing chain, or GENESIS_HASH for a new file."""
    if not path.exists():
        return GENESIS_HASH
    tip = GENESIS_HASH
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tip = str(row.get("entry_hash") or tip)
    except OSError as e:
        log.warning("Cannot read chain tip from %s: %s", path, e)
    return tip


def append_chained(path: Path, entry: dict) -> dict:
    """Append an entry to a hash-chained JSONL log. Returns the written entry.

    The lock covers read-modify-write of the in-memory tip: several async tasks
    log concurrently in one process, and two entries claiming the same prev_hash
    would look like tampering to the verifier.
    """
    with _chain_lock:
        tip = _chain_tips.get(str(path))
        if tip is None:
            tip = _last_hash_in_file(path)
        entry["prev_hash"] = tip
        entry["entry_hash"] = entry_hash(entry)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        _chain_tips[str(path)] = entry["entry_hash"]
    return entry


def verify_chain(path: Path) -> tuple[bool, str]:
    """Verify a chained log. Returns (ok, detail) naming the first bad line.

    Entries written before chaining existed have no hash. A leading run of them
    is history, not tampering, so it is skipped and counted — otherwise every
    existing installation would report a broken chain the moment it upgraded. An
    unchained entry appearing *after* a chained one is a different matter: that
    is a line someone inserted, and it fails.
    """
    if not path.exists():
        return True, f"{path.name}: no log yet"

    expected_prev = GENESIS_HASH
    checked = 0
    legacy = 0
    started = False

    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                return False, f"{path.name}:{number}: not valid JSON"

            if "entry_hash" not in row:
                if started:
                    return False, f"{path.name}:{number}: unchained entry inserted after chaining began"
                legacy += 1
                continue

            if not started:
                # First chained entry: it links to genesis on a fresh log, or to
                # nothing verifiable when it follows legacy entries.
                started = True
                expected_prev = row.get("prev_hash", GENESIS_HASH) if legacy else GENESIS_HASH

            if row.get("prev_hash") != expected_prev:
                return False, f"{path.name}:{number}: broken link (entry removed or reordered)"
            if entry_hash(row) != row["entry_hash"]:
                return False, f"{path.name}:{number}: content was modified"

            expected_prev = row["entry_hash"]
            checked += 1

    detail = f"{path.name}: {checked} entries verified"
    if legacy:
        detail += f", {legacy} pre-chain entries skipped"
    return True, detail


def verify_audit_logs(audit_dir: Path | None = None) -> list[tuple[str, bool, str]]:
    """Verify every chained log in a directory. Returns (name, ok, detail) rows."""
    directory = audit_dir or _get_audit_dir()
    return [(name, *verify_chain(directory / name)) for name in CHAINED_LOGS]


def reset_chain_cache() -> None:
    """Forget cached chain tips (tests, or after moving the audit directory)."""
    with _chain_lock:
        _chain_tips.clear()


def _get_audit_dir() -> Path:
    global _audit_dir
    target = Path(settings.db_path).parent / "logs"
    if _audit_dir != target:
        _audit_dir = target
        _audit_dir.mkdir(parents=True, exist_ok=True)
    return _audit_dir


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~3.5 chars per token for mixed RU/EN."""
    return math.ceil(len(text) / 3.5)


def set_tool_audit_context(**context: str) -> Token:
    """Set per-request context for durable tool-call audit events."""
    clean = {key: str(value) for key, value in context.items() if value is not None}
    return _tool_audit_context.set(clean)


def reset_tool_audit_context(token: Token) -> None:
    _tool_audit_context.reset(token)


def get_tool_audit_context() -> dict[str, str]:
    """Current per-request context (agent, thread_id, session_id, ...) or {}.

    Lets tools that run mid-invocation recover which chat/thread they belong to
    (e.g. schedule_task needs the originating chat to deliver the reminder).
    """
    return dict(_tool_audit_context.get())


def redact_secrets(text: str) -> str:
    """Replace secret-like substrings (bearer tokens, sk- keys, key= params).

    Unlike ``_redact_string`` this neither masks PII nor truncates, so callers
    that must preserve full content (exported bundles) can still strip
    credentials.
    """
    redacted = text
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _redact_string(value: str, *, max_len: int = 500) -> str:
    redacted = mask_pii(redact_secrets(value))
    if len(redacted) > max_len:
        return f"{redacted[: max_len - 3]}..."
    return redacted


def redact_tool_payload(value: Any, key: str = "") -> Any:
    """Redact secret-like fields before tool args/results reach storage or UI."""
    key_name = key.lower().replace("-", "_")
    if key_name in SECRET_FIELD_NAMES or key_name.endswith(("_token", "_secret", "_password", "_api_key", "_key")):
        return "***REDACTED***"
    if isinstance(value, dict):
        return {str(k): redact_tool_payload(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_tool_payload(item) for item in value[:20]]
    if isinstance(value, tuple):
        return [redact_tool_payload(item) for item in value[:20]]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _summarize_payload(value: Any, *, max_len: int = 500) -> str:
    redacted = redact_tool_payload(value)
    if isinstance(redacted, str):
        return _redact_string(redacted, max_len=max_len)
    try:
        rendered = json.dumps(redacted, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        rendered = str(redacted)
    return _redact_string(rendered, max_len=max_len)


def _infer_tool_capability(tool_name: str) -> str:
    name = tool_name.lower()
    if name.startswith("delegate_to_"):
        return "delegation"
    if name.startswith("mcp_"):
        return "mcp"
    if name in {"load_skill", "load_skill_reference", "approve_skill", "import_skill_from_source"}:
        return "skills"
    if "memory" in name or "session_search" in name:
        return "memory"
    if "browser" in name or "search" in name or "fetch" in name or "exa" in name or "brave" in name:
        return "research"
    if "expense" in name or "budget" in name or "tranche" in name:
        return "finance"
    if "server" in name or "ssh" in name:
        return "server_ops"
    if "dynamic" in name or "create_new_tool" in name:
        return "dynamic_tools"
    return "tools"


def _tool_event_status(event: str, payload: dict[str, Any]) -> str:
    if event == "tool_call":
        return "called"
    content = str(payload.get("content", ""))
    if content.startswith("[BLOCKED]") or content.lower().startswith("blocked:"):
        return "blocked"
    if payload.get("ok") is False or content.startswith("[ERROR]"):
        return "error"
    return "ok"


def log_tool_event(event: str, payload: dict[str, Any]) -> None:
    """Persist a tool-call lifecycle event to ``tool_calls.jsonl``.

    This stores only redacted summaries. Raw args/results never leave memory.
    """
    try:
        name = str(payload.get("name") or "unknown")
        status = _tool_event_status(event, payload)
        context = _tool_audit_context.get()
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "event": event,
            "status": status,
            "tool": name,
            "capability": str(payload.get("capability") or _infer_tool_capability(name)),
            "approval_status": "blocked" if status == "blocked" else "not_required",
            "call_id": str(payload.get("call_id") or payload.get("id") or ""),
            "turn": payload.get("turn"),
            "agent": context.get("agent", settings.agent_name),
            "thread_id": context.get("thread_id", ""),
            "session_id": context.get("session_id", ""),
            "user_id": context.get("user_id", ""),
            "source_kind": context.get("source_kind", ""),
            "args_summary": _summarize_payload(payload.get("args", {})),
            "result_summary": _summarize_payload(payload.get("content", "")) if event == "tool_result" else "",
            "model_result_summary": (
                _summarize_payload(payload.get("model_content", ""))
                if event == "tool_result" and "model_content" in payload
                else ""
            ),
            "compressed": bool(payload.get("compressed", False)),
            "raw_content_chars": payload.get("raw_content_chars"),
            "model_content_chars": payload.get("model_content_chars"),
            "error": status in {"error", "blocked"},
            "duration_ms": payload.get("duration_ms"),
            "cost_usd": payload.get("cost_usd"),
            "input_tokens": payload.get("input_tokens"),
            "output_tokens": payload.get("output_tokens"),
        }

        append_chained(_get_audit_dir() / "tool_calls.jsonl", entry)
    except Exception as e:
        log.debug("Tool audit logging failed: %s", e)


def log_credential_event(*, site: str, event: str, ok: bool, purpose: str = "", detail: str = "") -> None:
    """Record that a stored credential was written, used, or removed.

    The value is never here — that is the point. A reader gets which site, what
    happened and whether it worked, chained so that removing a line shows up.
    Answering "when did it use my password, and what for" is the whole job.

    A failure to write is logged loudly but does not block the caller: the
    alternative is that a full disk locks the owner out of their own accounts.
    """
    try:
        context = _tool_audit_context.get()
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "event": event,
            "site": site,
            "purpose": purpose,
            "ok": ok,
            "detail": _redact_string(detail, max_len=200) if detail else "",
            "agent": context.get("agent", settings.agent_name),
            "thread_id": context.get("thread_id", ""),
            "session_id": context.get("session_id", ""),
        }
        append_chained(_get_audit_dir() / "credentials.jsonl", entry)
    except Exception as e:
        log.warning("Credential audit logging failed for %s/%s: %s", site, event, e)


def log_request(
    *,
    user_id: str,
    session_id: str,
    tier: str,
    input_text: str,
    output_text: str,
    duration_ms: int,
    agent_path: str = "",
    blocked: bool = False,
) -> None:
    """Log a request to audit and cost JSONL files."""
    try:
        input_tokens = _estimate_tokens(input_text)
        output_tokens = _estimate_tokens(output_text)
        costs = COST_TABLE.get(tier, COST_TABLE["standard"])
        approx_cost = (input_tokens * costs["input"] + output_tokens * costs["output"]) / 1_000_000

        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        audit_dir = _get_audit_dir()

        # Audit log (detailed)
        audit_entry = {
            "ts": ts,
            "user_id": user_id,
            "session_id": session_id,
            "tier": tier,
            "agent_path": agent_path,
            "blocked": blocked,
            "duration_ms": duration_ms,
            "input_len": len(input_text),
            "output_len": len(output_text),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "approx_cost_usd": round(approx_cost, 6),
            "input_preview": mask_pii(input_text)[:100],
            "output_preview": mask_pii(output_text)[:100],
        }

        append_chained(audit_dir / "audit.jsonl", audit_entry)

        # Cost log (compact, for aggregation)
        cost_entry = {
            "ts": ts,
            "tier": tier,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": round(approx_cost, 6),
        }

        with open(audit_dir / "cost.jsonl", "a") as f:
            f.write(json.dumps(cost_entry) + "\n")

        log.debug(
            "Audit: tier=%s, tokens=%d+%d, cost=$%.6f, duration=%dms",
            tier,
            input_tokens,
            output_tokens,
            approx_cost,
            duration_ms,
        )

    except Exception as e:
        log.error("Audit logging failed: %s", e)


def get_daily_cost() -> dict:
    """Get today's cost summary from cost.jsonl."""
    today = time.strftime("%Y-%m-%d")
    total_cost = 0.0
    total_requests = 0
    total_input_tokens = 0
    total_output_tokens = 0

    cost_file = _get_audit_dir() / "cost.jsonl"
    if not cost_file.exists():
        return {"date": today, "cost_usd": 0, "requests": 0, "input_tokens": 0, "output_tokens": 0}

    try:
        with open(cost_file) as f:
            for line in f:
                entry = json.loads(line)
                if entry.get("ts", "").startswith(today):
                    total_cost += entry.get("cost_usd", 0)
                    total_requests += 1
                    total_input_tokens += entry.get("input_tokens", 0)
                    total_output_tokens += entry.get("output_tokens", 0)
    except Exception as e:
        log.error("Cost aggregation failed: %s", e)

    return {
        "date": today,
        "cost_usd": round(total_cost, 4),
        "requests": total_requests,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
    }
