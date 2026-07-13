"""Fleet API — one view over the whole swarm.

Every agent's dashboard is per-process, but the swarm ledger (swarm.db) is
shared — so any single dashboard can render the entire fleet from it: who
spoke when, who wins claims, hand-offs, councils, and memory requests.

Optional live health probes: set FLEET_HEALTH_PORTS="kronos=8788,nexus=8794"
to also ping each agent's webhook /health (localhost). Without it the view
degrades to ledger-derived liveness (last_seen).
"""

import asyncio
import os
import sqlite3
import time
from pathlib import Path

import aiohttp
from fastapi import APIRouter, Depends

from dashboard.auth import verify_token
from kronos.config import settings
from kronos.group_router import AGENT_PROFILES

router = APIRouter(prefix="/api/fleet", tags=["fleet"], dependencies=[Depends(verify_token)])

DAY_SECONDS = 24 * 3600
TIMELINE_LIMIT = 30
SPARK_BUCKETS = 24  # hourly buckets over the last 24h


def _rows(query: str, params: tuple = ()) -> list[dict]:
    db_path = Path(settings.swarm_db_path)
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(query, params).fetchall()]
    except sqlite3.Error:
        return []


def _health_ports() -> dict[str, int]:
    raw = os.environ.get("FLEET_HEALTH_PORTS", "")
    ports: dict[str, int] = {}
    for pair in raw.split(","):
        name, _, port = pair.strip().partition("=")
        if name and port.isdigit():
            ports[name] = int(port)
    return ports


async def _probe_health(ports: dict[str, int]) -> dict[str, dict]:
    """Ping each agent's webhook /health on localhost, 2s budget."""
    if not ports:
        return {}

    async def one(name: str, port: int) -> tuple[str, dict]:
        try:
            timeout = aiohttp.ClientTimeout(total=2)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                    body = await resp.json(content_type=None)
                    return name, {
                        "reachable": True,
                        "status": body.get("status", "unknown"),
                        "telegram_connected": bool(body.get("telegram_connected")),
                    }
        except Exception:
            return name, {"reachable": False}

    results = await asyncio.gather(*(one(n, p) for n, p in ports.items()))
    return dict(results)


def _agent_rollup(now: float) -> dict[str, dict]:
    """Per-agent aggregates from the shared ledger."""
    since = now - DAY_SECONDS
    rollup: dict[str, dict] = {}

    for row in _rows(
        "SELECT agent_name, MAX(created_at) AS last_seen, "
        "SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS messages_24h "
        "FROM swarm_messages WHERE sender_type = 'agent' AND agent_name IS NOT NULL "
        "GROUP BY agent_name",
        (since,),
    ):
        rollup[row["agent_name"]] = {
            "last_seen": row["last_seen"],
            "messages_24h": row["messages_24h"] or 0,
        }

    for row in _rows(
        "SELECT agent_name, "
        "SUM(CASE WHEN state = 'sent' THEN 1 ELSE 0 END) AS won, "
        "SUM(CASE WHEN state IN ('cancelled', 'expired') THEN 1 ELSE 0 END) AS yielded "
        "FROM reply_claims WHERE created_at >= ? GROUP BY agent_name",
        (since,),
    ):
        rollup.setdefault(row["agent_name"], {})
        rollup[row["agent_name"]]["replies_won_24h"] = row["won"] or 0
        rollup[row["agent_name"]]["claims_yielded_24h"] = row["yielded"] or 0

    for row in _rows("SELECT to_agent, COUNT(*) AS n FROM handoffs WHERE state = 'pending' GROUP BY to_agent"):
        rollup.setdefault(row["to_agent"], {})
        rollup[row["to_agent"]]["handoffs_pending"] = row["n"]

    return rollup


def _activity_sparkline(now: float) -> dict[str, list[int]]:
    """Hourly agent-message counts per agent over the last 24h."""
    since = now - DAY_SECONDS
    sparks: dict[str, list[int]] = {}
    for row in _rows(
        "SELECT agent_name, CAST((created_at - ?) / 3600 AS INTEGER) AS bucket, COUNT(*) AS n "
        "FROM swarm_messages "
        "WHERE sender_type = 'agent' AND agent_name IS NOT NULL AND created_at >= ? "
        "GROUP BY agent_name, bucket",
        (since, since),
    ):
        spark = sparks.setdefault(row["agent_name"], [0] * SPARK_BUCKETS)
        bucket = min(max(int(row["bucket"]), 0), SPARK_BUCKETS - 1)
        spark[bucket] = row["n"]
    return sparks


def _timeline() -> list[dict]:
    """Merged coordination feed: replies won, hand-offs, councils, memory asks."""
    events: list[dict] = []

    for row in _rows(
        "SELECT agent_name, tier, reason, created_at FROM reply_claims "
        "WHERE state = 'sent' ORDER BY created_at DESC LIMIT ?",
        (TIMELINE_LIMIT,),
    ):
        events.append(
            {
                "kind": "reply",
                "ts": row["created_at"],
                "from_agent": row["agent_name"],
                "to_agent": "",
                "text": row["reason"] or f"Tier {row['tier']} reply",
                "state": "sent",
            }
        )

    for row in _rows(
        "SELECT from_agent, to_agent, context, state, created_at FROM handoffs ORDER BY created_at DESC LIMIT ?",
        (TIMELINE_LIMIT,),
    ):
        events.append(
            {
                "kind": "handoff",
                "ts": row["created_at"],
                "from_agent": row["from_agent"],
                "to_agent": row["to_agent"],
                "text": row["context"],
                "state": row["state"],
            }
        )

    for row in _rows(
        "SELECT initiator, participants, question, state, created_at FROM council_sessions "
        "ORDER BY created_at DESC LIMIT ?",
        (TIMELINE_LIMIT,),
    ):
        events.append(
            {
                "kind": "council",
                "ts": row["created_at"],
                "from_agent": row["initiator"],
                "to_agent": row["participants"],
                "text": row["question"],
                "state": row["state"],
            }
        )

    for row in _rows(
        "SELECT from_agent, to_agent, query, state, created_at FROM memory_requests ORDER BY created_at DESC LIMIT ?",
        (TIMELINE_LIMIT,),
    ):
        events.append(
            {
                "kind": "memory",
                "ts": row["created_at"],
                "from_agent": row["from_agent"],
                "to_agent": row["to_agent"],
                "text": row["query"],
                "state": row["state"],
            }
        )

    events.sort(key=lambda item: item["ts"] or 0, reverse=True)
    return events[:TIMELINE_LIMIT]


def _totals(now: float) -> dict:
    since = now - DAY_SECONDS
    user_messages = _rows(
        "SELECT COUNT(*) AS n FROM swarm_messages WHERE sender_type != 'agent' AND created_at >= ?",
        (since,),
    )
    agent_messages = _rows(
        "SELECT COUNT(*) AS n FROM swarm_messages WHERE sender_type = 'agent' AND created_at >= ?",
        (since,),
    )
    active_councils = _rows("SELECT COUNT(*) AS n FROM council_sessions WHERE state IN ('gathering', 'synthesizing')")
    pending_handoffs = _rows("SELECT COUNT(*) AS n FROM handoffs WHERE state = 'pending'")
    pending_memory = _rows("SELECT COUNT(*) AS n FROM memory_requests WHERE state = 'pending'")
    shared_facts = _rows("SELECT COUNT(*) AS n FROM shared_user_facts")
    metrics = {row["metric"]: row["value"] for row in _rows("SELECT metric, value FROM swarm_metrics")}

    def n(rows: list[dict]) -> int:
        return int(rows[0]["n"]) if rows else 0

    return {
        "user_messages_24h": n(user_messages),
        "agent_messages_24h": n(agent_messages),
        "active_councils": n(active_councils),
        "pending_handoffs": n(pending_handoffs),
        "pending_memory_requests": n(pending_memory),
        "shared_facts": n(shared_facts),
        "metrics": metrics,
    }


@router.get("/overview")
async def fleet_overview():
    """The whole swarm at a glance, derived from the shared ledger."""
    now = time.time()
    rollup = _agent_rollup(now)
    sparks = _activity_sparkline(now)
    health = await _probe_health(_health_ports())

    agents = []
    known = set(AGENT_PROFILES) | set(rollup)
    for name in sorted(known):
        profile = AGENT_PROFILES.get(name, {})
        stats = rollup.get(name, {})
        agents.append(
            {
                "name": name,
                "username": profile.get("username", ""),
                "role": profile.get("role", ""),
                "is_me": name == settings.agent_name,
                "last_seen": stats.get("last_seen"),
                "messages_24h": stats.get("messages_24h", 0),
                "replies_won_24h": stats.get("replies_won_24h", 0),
                "claims_yielded_24h": stats.get("claims_yielded_24h", 0),
                "handoffs_pending": stats.get("handoffs_pending", 0),
                "sparkline": sparks.get(name, [0] * SPARK_BUCKETS),
                "health": health.get(name),
            }
        )

    return {
        "agents": agents,
        "totals": _totals(now),
        "timeline": _timeline(),
        "generated_at": now,
        "health_probes_configured": bool(_health_ports()),
    }
