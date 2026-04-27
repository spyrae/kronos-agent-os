"""Swarm coordination visualizer API."""

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends

from dashboard.auth import verify_token
from kronos.config import settings

router = APIRouter(prefix="/api/swarm", tags=["swarm"], dependencies=[Depends(verify_token)])


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


def _demo_runs() -> list[dict]:
    return [{
        "id": "demo-launch-plan",
        "title": "Demo: launch plan arbitration",
        "status": "demo",
        "created_at": "2026-04-27T09:00:00+00:00",
        "updated_at": "2026-04-27T09:05:00+00:00",
        "summary": "Four roles split research, critique, operations, and synthesis for an open-source launch.",
        "roles": [
            {"agent": "researcher", "role": "Researcher", "tier": 2, "status": "sent", "task": "Find comparable OSS launch patterns."},
            {"agent": "critic", "role": "Critic", "tier": 2, "status": "sent", "task": "Find setup and trust risks."},
            {"agent": "operator", "role": "Operator", "tier": 2, "status": "sent", "task": "Convert decision into tasks."},
            {"agent": "synthesizer", "role": "Synthesizer", "tier": 1, "status": "winner", "task": "Return one final answer."},
        ],
        "steps": [
            {"agent": "researcher", "kind": "intermediate", "text": "Open-source launches spread when quickstart, demos, and trust docs are obvious.", "status": "sent"},
            {"agent": "critic", "kind": "vote", "text": "Risk: tool permissions must be legible before viral demos.", "status": "sent"},
            {"agent": "operator", "kind": "intermediate", "text": "Plan becomes docs, templates, dashboard, Docker smoke, and demo fixtures.", "status": "sent"},
            {"agent": "synthesizer", "kind": "decision", "text": "Ship Agent OS framing with optional swarm visualizer, not swarm-only positioning.", "status": "winner"},
        ],
        "final": "One synthesized launch plan with risks called out and tasks mapped to Linear.",
        "metrics": {"claims": 4, "sent": 4, "active": 0, "duplicate_replies_avoided": 2},
        "demo": True,
    }]


def _metric(name: str) -> int:
    rows = _rows("SELECT value FROM swarm_metrics WHERE metric = ?", (name,))
    return int(rows[0]["value"]) if rows else 0


def _build_runs() -> list[dict]:
    claims = _rows(
        """
        SELECT id, chat_id, topic_id, root_msg_id, trigger_msg_id, agent_name,
               tier, eta_ts, state, reason, reply_msg_id, created_at
        FROM reply_claims
        ORDER BY created_at DESC
        """
    )
    if not claims:
        return _demo_runs()

    grouped: dict[tuple[int, int, int], list[dict]] = {}
    for claim in claims:
        key = (claim["chat_id"], claim["topic_id"], claim["root_msg_id"])
        grouped.setdefault(key, []).append(claim)

    runs = []
    for (chat_id, topic_id, root_msg_id), group in grouped.items():
        group = sorted(group, key=lambda item: (item["tier"], item["eta_ts"], item["agent_name"]))
        winner = next((item for item in group if item["state"] == "sent"), None) or group[0]
        messages = _rows(
            """
            SELECT msg_id, reply_to_msg_id, sender_type, agent_name, text, created_at
            FROM swarm_messages
            WHERE chat_id = ? AND topic_id = ?
              AND (msg_id = ? OR reply_to_msg_id = ? OR msg_id IN ({placeholders}))
            ORDER BY created_at ASC
            """.format(placeholders=",".join("?" for _ in group) or "0"),
            (chat_id, topic_id, root_msg_id, root_msg_id, *[item["trigger_msg_id"] for item in group]),
        )
        final_message = next(
            (msg for msg in reversed(messages) if msg.get("sender_type") == "agent" and msg.get("agent_name") == winner["agent_name"]),
            None,
        )
        roles = [
            {
                "agent": item["agent_name"],
                "role": f"Tier {item['tier']} responder",
                "tier": item["tier"],
                "status": "winner" if item["agent_name"] == winner["agent_name"] else item["state"],
                "task": item.get("reason") or f"Reply claim for message {item['trigger_msg_id']}",
            }
            for item in group
        ]
        steps = [
            {
                "agent": item["agent_name"],
                "kind": "claim",
                "text": item.get("reason") or f"ETA {round(float(item['eta_ts'] or 0))}",
                "status": "winner" if item["agent_name"] == winner["agent_name"] else item["state"],
            }
            for item in group
        ]
        steps.extend([
            {
                "agent": msg.get("agent_name") or msg.get("sender_type"),
                "kind": "message",
                "text": msg.get("text", "")[:500],
                "status": "sent" if msg.get("sender_type") == "agent" else "observed",
            }
            for msg in messages
        ])
        runs.append({
            "id": f"{chat_id}:{topic_id}:{root_msg_id}",
            "title": f"Chat {chat_id} / root {root_msg_id}",
            "status": "active" if any(item["state"] == "claimed" for item in group) else "completed",
            "created_at": str(min(item["created_at"] for item in group)),
            "updated_at": str(max(item["created_at"] for item in group)),
            "summary": f"{len(group)} claims, winner: {winner['agent_name']}",
            "roles": roles,
            "steps": steps[:40],
            "final": (final_message or {}).get("text", ""),
            "metrics": {
                "claims": len(group),
                "sent": sum(1 for item in group if item["state"] == "sent"),
                "active": sum(1 for item in group if item["state"] == "claimed"),
                "duplicate_replies_avoided": _metric("duplicate_replies_avoided"),
            },
            "demo": False,
        })
    return runs


@router.get("/runs")
async def list_swarm_runs():
    runs = _build_runs()
    return {
        "runs": runs,
        "total": len(runs),
        "demo": all(run.get("demo") for run in runs),
    }
