"""Skill Create — auto-detect repeatable patterns and create draft skills.

Called by self_improve as a second analysis phase.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from kronos.config import settings
from kronos.cron.notify import TOPIC_GENERAL, send_bot_api
from kronos.llm import ModelTier, get_model
from kronos.skills.store import SkillStore

log = logging.getLogger("kronos.cron.skill_create")

LOOKBACK_DAYS = 7
MIN_TOOL_CALLS = 5
MIN_SUPERVISOR_STEPS = 3


def _load_recent_audit_entries(lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """Load audit entries from the last N days."""
    audit_file = Path(settings.db_path).parent / "logs" / "audit.jsonl"
    if not audit_file.exists():
        return []

    cutoff = time.time() - (lookback_days * 86400)
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
    return entries


def _filter_complex_sessions(entries: list[dict]) -> list[dict]:
    """Filter sessions with high tool call count or supervisor steps."""
    return [
        e
        for e in entries
        if e.get("tool_calls_count", 0) >= MIN_TOOL_CALLS
        or e.get("supervisor_steps", 0) >= MIN_SUPERVISOR_STEPS
    ]


def _simple_token_overlap(text1: str, text2: str) -> float:
    """Simple token overlap ratio for deduplication."""
    tokens1 = set(text1.lower().split())
    tokens2 = set(text2.lower().split())
    if not tokens1 or not tokens2:
        return 0.0
    intersection = tokens1 & tokens2
    return len(intersection) / min(len(tokens1), len(tokens2))


async def analyze_for_new_skills(entries: list[dict] | None = None) -> str | None:
    """Analyze recent sessions for repeatable patterns that could become skills.

    Returns skill name if created, None otherwise.
    """
    if entries is None:
        entries = _load_recent_audit_entries()

    complex_sessions = _filter_complex_sessions(entries)
    if len(complex_sessions) < 2:
        log.info(
            "Not enough complex sessions (%d) for skill creation",
            len(complex_sessions),
        )
        return None

    # Build session summaries for LLM
    summaries = []
    for e in complex_sessions[-10:]:
        inp = e.get("input_preview", "")[:150]
        out = e.get("output_preview", "")[:100]
        tools = e.get("tool_calls_count", 0)
        steps = e.get("supervisor_steps", 0)
        summaries.append(
            f"[tools={tools}, steps={steps}] User: {inp} → Agent: {out}"
        )

    sessions_text = "\n".join(summaries)

    # Load existing skills for dedup
    skill_store = SkillStore(settings.workspace_path)
    existing_skills = [
        f"{s.name}: {s.description}" for s in skill_store.list_skills()
    ]
    existing_text = (
        "\n".join(existing_skills) if existing_skills else "(нет существующих скиллов)"
    )

    prompt = f"""Проанализируй сложные сессии агента и определи, есть ли повторяемый паттерн,
который стоит оформить как навык (skill).

Сессии с высоким числом tool-вызовов или шагов маршрутизации:
{sessions_text}

Существующие навыки (не дублируй):
{existing_text}

Если видишь повторяемый паттерн — верни JSON:
{{
    "found": true,
    "name": "skill-name-kebab-case",
    "description": "Краткое описание (1 строка)",
    "trigger": "Когда использовать",
    "protocol": "Пошаговый протокол (3-7 шагов)",
    "tools": ["tool1", "tool2"]
}}

Если паттернов нет — верни: {{"found": false}}

Правила:
- Только ОДИН навык за раз
- Название — kebab-case, 2-3 слова
- Паттерн должен повторяться минимум 2 раза в данных
- Не предлагай то, что уже покрыто существующими навыками
"""

    model = get_model(ModelTier.LITE)
    from langchain_core.messages import HumanMessage

    response = model.invoke([HumanMessage(content=prompt)])
    reply = (
        response.content if isinstance(response.content, str) else str(response.content)
    )

    # Parse JSON from response
    try:
        # Extract JSON from possible markdown
        json_match = re.search(r"\{[^{}]*\"found\"[^{}]*\}", reply, re.DOTALL)
        if not json_match:
            log.info("No JSON found in skill analysis response")
            return None
        data = json.loads(json_match.group())
    except (json.JSONDecodeError, AttributeError):
        log.warning("Failed to parse skill analysis response")
        return None

    if not data.get("found"):
        log.info("No repeatable patterns found for skill creation")
        return None

    name = data.get("name", "").strip()
    if not name:
        return None

    # Dedup check: fuzzy match against existing skills
    for skill in skill_store.list_skills():
        overlap = _simple_token_overlap(
            f"{name} {data.get('description', '')}",
            f"{skill.name} {skill.description}",
        )
        if overlap > 0.6:
            log.info(
                "Skill '%s' too similar to existing '%s' (overlap=%.2f)",
                name,
                skill.name,
                overlap,
            )
            return None

    # Create draft skill
    now = datetime.now(timezone.utc).isoformat()
    protocol = data.get("protocol", "")
    trigger = data.get("trigger", "")
    tools_list = data.get("tools", [])
    description = data.get("description", "")

    content = f"""# {name}

## Trigger
{trigger}

## Protocol
{protocol}

## Tools
{', '.join(tools_list) if tools_list else 'N/A'}
"""

    meta = {
        "name": name,
        "description": description,
        "status": "draft",
        "created_by": "auto",
        "created_at": now,
        "version": "1",
    }

    skill_store.add_skill(name, content, meta)

    send_bot_api(
        f"Draft skill создан: <b>{name}</b>\n{description}\n\n"
        f"Скажи 'одобрить skill {name}' для активации.",
        topic_id=TOPIC_GENERAL,
    )

    log.info("Draft skill created: %s", name)
    return name
