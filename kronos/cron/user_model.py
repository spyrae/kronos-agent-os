"""User Model — dialectical user modeling via audit analysis + LLM.

Two phases:
1. Quantitative: pure Python analytics on audit.jsonl (patterns, stats)
2. Qualitative: LLM analyzes conversations to build/update hypotheses
   about user preferences, communication style, goals, and context

Hypotheses are stored in USER-MODEL.md and loaded into agent's context.
Each run validates existing hypotheses against new data and refines them.
"""

import json
import logging
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from kronos.config import settings
from kronos.cron.notify import TOPIC_GENERAL, send_bot_api
from kronos.llm import ModelTier, get_model

log = logging.getLogger("kronos.cron.user_model")

LOOKBACK_DAYS = 14
MIN_ENTRIES = 5

MODEL_PROMPT = """Ты — аналитик поведения пользователя. Построй диалектическую модель на основе данных.

Количественные данные:
{stats}

Последние разговоры:
{conversations}

Обратная связь (реакции):
{feedback}

Текущая модель:
{current_model}

Задача — обновить модель в диалектическом формате:

1. **Beliefs** — убеждения пользователя с confidence (0.0-1.0)
   - Для каждого: evidence (что подтверждает) + tensions (что противоречит)
   - Если confidence изменился — объясни почему

2. **Motivations** — глубинные мотивации (не поверхностные запросы)
   - Ищи паттерны: что стоит ЗА запросами?

3. **Decision Patterns** — как принимает решения
   - Тенденции + конкретные примеры из сессий

4. **Tensions** — неразрешённые противоречия
   - Конфликты между beliefs, мотивациями, действиями

5. **Evolution** — что изменилось с прошлого анализа
   - Новые beliefs, изменение confidence, разрешённые tensions

Формат ответа — строго markdown:

## Beliefs (confidence: 0.0-1.0)
- [0.95] Описание belief
  - Evidence: конкретные примеры из сессий
  - Tensions: что противоречит этому belief

## Motivations
- Мотивация с обоснованием

## Decision Patterns
- Паттерн: тенденция + примеры

## Tensions (unresolved)
- Противоречие между X и Y

## Evolution
- YYYY-MM: что изменилось

Русский язык. Конкретика, не абстракции. Каждый пункт — с evidence из данных.
"""


async def run_user_model() -> None:
    """Analyze audit log and update dialectical user model."""
    audit_file = Path(settings.db_path).parent / "logs" / "audit.jsonl"
    if not audit_file.exists():
        log.info("No audit log yet, skipping user-model")
        return

    cutoff = time.time() - (LOOKBACK_DAYS * 86400)
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

    if len(entries) < MIN_ENTRIES:
        log.info("Only %d entries (min %d), skipping", len(entries), MIN_ENTRIES)
        return

    # Phase 1: Quantitative analysis
    stats = _compute_stats(entries)

    # Phase 2: Conversation previews for qualitative analysis
    conversations = "\n".join(
        f"[{e.get('tier', '?')}] User: {e.get('input_preview', '')[:100]} → Agent: {e.get('output_preview', '')[:80]}"
        for e in entries[-30:]
    )

    # Enrich with session FTS data for deeper analysis
    session_context = ""
    try:
        from kronos.swarm_store import get_swarm
        swarm = get_swarm()
        # Search for decision-making patterns
        for query in ["решил", "выбрал", "предпочитаю", "хочу", "не хочу"]:
            results = swarm.search_sessions(
                query=query,
                agent_name=settings.agent_name,
                days=LOOKBACK_DAYS,
                limit=3,
            )
            for r in results:
                session_context += f"[{r['role']}] {r['content'][:150]}\n"
    except Exception:
        pass

    if session_context:
        conversations += f"\n\nКонтекст из сессий (поиск по решениям):\n{session_context}"

    # Collect feedback for richer signal
    feedback_text = ""
    try:
        from kronos.swarm_store import get_swarm
        swarm = get_swarm()
        satisfaction = swarm.get_satisfaction_rate(
            agent_name=settings.agent_name,
            days=LOOKBACK_DAYS,
        )
        negative = swarm.get_feedback(
            agent_name=settings.agent_name,
            reaction="negative",
            days=LOOKBACK_DAYS,
            limit=10,
        )
        feedback_text = (
            f"Satisfaction rate: {satisfaction['satisfaction_rate']}% "
            f"({satisfaction['positive']}👍 / {satisfaction['negative']}👎)\n"
        )
        if negative:
            feedback_text += "Негативные реакции на:\n"
            for fb in negative:
                feedback_text += f"- msg_id={fb['msg_id']}, {fb['emoji']}\n"
    except Exception as e:
        log.warning("Feedback collection failed (non-fatal): %s", e)
        feedback_text = "(feedback данные недоступны)"

    # Load current model (previous hypotheses)
    from kronos.workspace import ws
    model_path = ws.user_model
    current_model = ""
    if model_path.exists():
        current_model = model_path.read_text(encoding="utf-8")

    # Phase 3: LLM dialectical analysis
    prompt = MODEL_PROMPT.format(
        stats=stats,
        conversations=conversations,
        feedback=feedback_text,
        current_model=current_model or "(Первый анализ — модели ещё нет)",
    )

    model = get_model(ModelTier.STANDARD)
    from langchain_core.messages import HumanMessage
    response = model.invoke([HumanMessage(content=prompt)])
    new_model = response.content if isinstance(response.content, str) else str(response.content)

    if not new_model or len(new_model) < 100:
        log.warning("LLM returned empty model, keeping previous")
        return

    # Save updated model
    header = (
        f"# User Model\n\n"
        f"Updated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Period: {LOOKBACK_DAYS} days, {len(entries)} interactions\n\n"
    )
    model_path.write_text(header + new_model, encoding="utf-8")

    # Also save quantitative stats
    patterns_path = ws.user_patterns
    patterns_path.write_text(f"# User Patterns — Quantitative\n\nUpdated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}\n\n{stats}", encoding="utf-8")

    log.info("User model updated: %d entries, %d chars", len(entries), len(new_model))

    # Count hypotheses changes
    changes_section = ""
    if "## Changes" in new_model:
        changes_section = new_model[new_model.index("## Changes"):][:300]

    send_bot_api(
        f"🧠 User Model updated\n"
        f"Period: {LOOKBACK_DAYS}d, {len(entries)} interactions\n"
        f"{changes_section}",
        topic_id=TOPIC_GENERAL,
    )


def _compute_stats(entries: list[dict]) -> str:
    """Compute quantitative stats from audit entries."""
    hours = Counter()
    tiers = Counter()
    input_lengths = []
    output_lengths = []
    durations = []

    for e in entries:
        ts = e.get("ts", "")
        if ts:
            try:
                dt = datetime.fromisoformat(ts)
                hours[dt.hour] += 1
            except ValueError:
                pass

        tiers[e.get("tier", "unknown")] += 1
        input_lengths.append(e.get("input_len", 0))
        output_lengths.append(e.get("output_len", 0))
        durations.append(e.get("duration_ms", 0))

    peak_hours = [h for h, _ in hours.most_common(3)]
    avg_input = sum(input_lengths) / len(input_lengths) if input_lengths else 0
    avg_output = sum(output_lengths) / len(output_lengths) if output_lengths else 0
    avg_duration = sum(durations) / len(durations) if durations else 0
    short = sum(1 for l in input_lengths if l < 50)
    short_pct = (short / len(input_lengths) * 100) if input_lengths else 0

    return (
        f"Total: {len(entries)} interactions over {LOOKBACK_DAYS} days\n"
        f"Peak hours (UTC): {', '.join(f'{h}:00' for h in peak_hours)}\n"
        f"Avg message: {avg_input:.0f} chars, response: {avg_output:.0f} chars\n"
        f"Short (<50 chars): {short_pct:.0f}%\n"
        f"Tier distribution: {dict(tiers)}\n"
        f"Avg response time: {avg_duration:.0f}ms"
    )
