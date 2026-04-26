"""Heartbeat — periodic system check.

Reads HEARTBEAT.md tasks + Notion DB, sends to LLM for analysis.
Logs internally; only notifies the user on real problems (with cooldown).
"""

import json
import logging
import os
import urllib.request
from datetime import UTC, datetime

from kronos.config import settings
from kronos.cron.notify import TOPIC_GENERAL, send_bot_api
from kronos.llm import ModelTier, get_model

log = logging.getLogger("kronos.cron.heartbeat")

# Minimum interval between Telegram notifications per agent (seconds).
_NOTIFY_COOLDOWN_SECONDS = 4 * 3600  # 4 hours
_last_notify_ts: float = 0.0


def _query_notion_tasks() -> list[dict]:
    """Query Notion database for current tasks."""
    token = os.environ.get("NOTION_API_TOKEN", "")  # noqa: F811
    db_id = os.environ.get("NOTION_DB_ID", "")
    if not token or not db_id:
        return []

    try:
        payload = json.dumps({}).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.notion.com/v1/databases/{db_id}/query",
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Notion-Version": "2022-06-28",
            },
        )
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())

        tasks = []
        for page in data.get("results", []):
            props = page.get("properties", {})
            task = {}
            for key, val in props.items():
                pt = val.get("type", "")
                if pt == "title":
                    task[key] = "".join(t.get("plain_text", "") for t in val.get("title", []))
                elif pt == "status":
                    s = val.get("status")
                    task[key] = s.get("name", "") if s else ""
                elif pt == "date":
                    d = val.get("date")
                    task[key] = d.get("start", "") if d else ""
                elif pt == "select":
                    s = val.get("select")
                    task[key] = s.get("name", "") if s else ""
            if task:
                tasks.append(task)
        return tasks
    except Exception as e:
        log.error("Notion query failed: %s", e)
        return []


async def run_heartbeat() -> None:
    """Run heartbeat check."""
    # Load HEARTBEAT.md
    from kronos.workspace import ws
    hb_path = ws.heartbeat
    heartbeat_content = ""
    if hb_path.exists():
        heartbeat_content = hb_path.read_text(encoding="utf-8").strip()

    if not heartbeat_content:
        log.info("HEARTBEAT.md empty, skipping")
        return

    # Query Notion tasks
    tasks = _query_notion_tasks()
    tasks_text = ""
    if tasks:
        tasks_text = "\n\nCurrent tasks from Notion:\n"
        for t in tasks[:20]:
            tasks_text += f"- {t}\n"

    # Satisfaction rate
    satisfaction_text = ""
    try:
        from kronos.swarm_store import get_swarm
        swarm = get_swarm()
        satisfaction = swarm.get_satisfaction_rate(
            agent_name=settings.agent_name,
            days=7,
        )
        if satisfaction["total"] > 0:
            satisfaction_text = (
                f"\n\nSatisfaction rate (7d): {satisfaction['satisfaction_rate']}% "
                f"({satisfaction['positive']}👍 / {satisfaction['negative']}👎 / "
                f"{satisfaction['neutral']}🤷 — total {satisfaction['total']})"
            )
    except Exception:
        pass

    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    prompt = f"""Current time: {now}

Heartbeat tasks:
{heartbeat_content}
{tasks_text}{satisfaction_text}

You are a system health monitor. Review the data above and classify:

- "heartbeat: ok" — everything is normal, no concrete problems detected.
- "heartbeat: problem" followed by a brief description — ONLY if you see a
  CONCRETE, VERIFIABLE problem: an overdue deadline with a specific date,
  a metric that actually dropped (with numbers), a task explicitly blocked.

IMPORTANT: Do NOT flag general recommendations, hypothetical risks, or things
that "should be checked". Only flag problems you can prove from the data above.
If you have no data to verify a problem — it's "heartbeat: ok".

Answer in Russian."""

    model = get_model(ModelTier.LITE)
    from langchain_core.messages import HumanMessage
    response = model.invoke([HumanMessage(content=prompt)])
    reply = response.content if isinstance(response.content, str) else str(response.content)

    reply_lower = reply.lower()

    if "heartbeat: ok" in reply_lower or "heartbeat:ok" in reply_lower:
        log.info("Heartbeat: all ok")
        return

    # Problem detected — log always, notify with cooldown
    log.warning("Heartbeat: problem detected — %s", reply[:200])

    global _last_notify_ts
    now_ts = datetime.now(UTC).timestamp()
    if now_ts - _last_notify_ts < _NOTIFY_COOLDOWN_SECONDS:
        log.info("Heartbeat: skipping notification (cooldown)")
        return

    _last_notify_ts = now_ts
    send_bot_api(f"💓 Heartbeat\n\n{reply}", topic_id=TOPIC_GENERAL)
