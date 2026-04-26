"""Self-Improve — daily analysis of agent sessions for learning.

Reads audit log, identifies ONE concrete improvement, saves as learning record.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from kronos.config import settings
from kronos.cron.notify import send_bot_api, TOPIC_GENERAL
from kronos.llm import ModelTier, get_model
from kronos.swarm_store import get_swarm

log = logging.getLogger("kronos.cron.self_improve")

LOOKBACK_HOURS = 24
MAX_ENTRIES = 20


async def run_self_improve() -> None:
    """Analyze recent sessions and produce one learning record."""
    audit_file = Path(settings.db_path).parent / "logs" / "audit.jsonl"
    if not audit_file.exists():
        log.info("No audit log, skipping self-improve")
        return

    cutoff = time.time() - (LOOKBACK_HOURS * 3600)
    entries = []

    with open(audit_file) as f:
        for line in f:
            try:
                entry = json.loads(line)
                ts = entry.get("ts", "")
                if ts:
                    dt = datetime.fromisoformat(ts)
                    if dt.timestamp() > cutoff:
                        entries.append(entry)
            except (json.JSONDecodeError, ValueError):
                continue

    if len(entries) < 3:
        log.info("Only %d entries in last %dh, skipping", len(entries), LOOKBACK_HOURS)
        return

    # Prioritize sessions with negative feedback
    try:
        swarm = get_swarm()
        negative_feedback = swarm.get_feedback(
            agent_name=settings.agent_name,
            reaction="negative",
            days=1,  # last day only for self-improve
            limit=10,
        )
        if negative_feedback:
            negative_msg_ids = {f["msg_id"] for f in negative_feedback}
            # Move entries matching negative feedback to the front
            neg_entries = []
            other_entries = []
            for e in entries:
                # Check if any audit entry corresponds to a negatively-rated response
                if any(str(mid) in str(e.get("output_preview", "")) for mid in negative_msg_ids):
                    neg_entries.append(e)
                else:
                    other_entries.append(e)
            if neg_entries:
                log.info("Prioritizing %d sessions with negative feedback", len(neg_entries))
                entries = neg_entries + other_entries
    except Exception as e:
        log.warning("Feedback prioritization failed (non-fatal): %s", e)

    # Take last N entries
    recent = entries[-MAX_ENTRIES:]
    sessions_text = ""
    for e in recent:
        inp = e.get("input_preview", "")[:80]
        out = e.get("output_preview", "")[:80]
        tier = e.get("tier", "?")
        dur = e.get("duration_ms", 0)
        sessions_text += f"[{tier}, {dur}ms] User: {inp} → Agent: {out}\n"

    # Load previous improvements for context
    from kronos.workspace import ws
    memory_dir = ws.self_improve_dir
    memory_dir.mkdir(parents=True, exist_ok=True)
    prev_improvements = []
    for f in sorted(memory_dir.glob("*.md"))[-5:]:
        prev_improvements.append(f.read_text(encoding="utf-8")[:200])

    prev_text = ""
    if prev_improvements:
        prev_text = "\n\nПредыдущие улучшения:\n" + "\n---\n".join(prev_improvements)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    prompt = f"""Проанализируй последние сессии агента Kronos и предложи ОДНО конкретное улучшение.

Сессии за последние {LOOKBACK_HOURS}ч ({len(recent)} записей):
{sessions_text}
{prev_text}

Формат ответа:
## Наблюдение
[Что заметил в сессиях]

## Улучшение
[Конкретное предложение]

## Действие
[Что конкретно изменить/добавить]

Правила:
- Только ОДНО улучшение, самое важное
- Конкретное, измеримое, реализуемое
- Не повторяй предыдущие улучшения
- Если всё хорошо — напиши "Нет улучшений, всё работает стабильно"
- Русский язык"""

    model = get_model(ModelTier.LITE)
    from langchain_core.messages import HumanMessage
    response = model.invoke([HumanMessage(content=prompt)])
    reply = response.content if isinstance(response.content, str) else str(response.content)

    if "нет улучшений" in reply.lower():
        log.info("Self-improve: no improvements needed")
        return

    # Save learning record
    record_path = memory_dir / f"{today}.md"
    record_path.write_text(f"# Self-Improvement — {today}\n\n{reply}", encoding="utf-8")
    log.info("Learning record saved: %s", record_path)

    send_bot_api(f"🧠 Self-improvement ({today})\n\n{reply}", topic_id=TOPIC_GENERAL)

    # Phase 2: Check for skill creation opportunities
    try:
        from kronos.cron.skill_create import analyze_for_new_skills

        created = await analyze_for_new_skills(entries=entries)
        if created:
            log.info("Self-improve also created draft skill: %s", created)
    except Exception as e:
        log.warning("Skill creation analysis failed (non-fatal): %s", e)
