"""Audit and cost logging — tracks every request with approximate cost.

Logs to JSONL files:
- audit.jsonl: full request/response audit trail
- cost.jsonl: cost tracking per request
"""

import json
import logging
import math
import time
from pathlib import Path

from kronos.config import settings

log = logging.getLogger("kronos.audit")

# DeepSeek V3 pricing (per 1M tokens)
COST_TABLE = {
    "lite": {"input": 0.27, "output": 1.10},
    "standard": {"input": 0.27, "output": 1.10},  # same model for now
    "blocked": {"input": 0, "output": 0},
}

_audit_dir: Path | None = None


def _get_audit_dir() -> Path:
    global _audit_dir
    if _audit_dir is None:
        _audit_dir = Path(settings.db_path).parent / "logs"
        _audit_dir.mkdir(parents=True, exist_ok=True)
    return _audit_dir


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~3.5 chars per token for mixed RU/EN."""
    return math.ceil(len(text) / 3.5)


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
            "input_preview": input_text[:100],
            "output_preview": output_text[:100],
        }

        with open(audit_dir / "audit.jsonl", "a") as f:
            f.write(json.dumps(audit_entry, ensure_ascii=False) + "\n")

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
            tier, input_tokens, output_tokens, approx_cost, duration_ms,
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
