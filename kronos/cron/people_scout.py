"""People Scout — weekly LinkedIn profile discovery.

Rotates focus weekly: US founders → EU founders → AI engineers → Indie hackers.
Uses LLM with web search knowledge, tracks seen profiles in SEEN.md.
"""

import logging
import re
from datetime import UTC, datetime

from kronos.config import settings
from kronos.cron.notify import TOPIC_DIGEST, send_bot_api
from kronos.llm import ModelTier, get_model

log = logging.getLogger("kronos.cron.people_scout")

FOCUS_ROTATION = [
    "US-based tech founders and startup CEOs",
    "EU-based tech founders and entrepreneurs",
    "AI/ML engineers and researchers",
    "Indie hackers and solo founders",
]


def _load_criteria() -> str:
    from kronos.workspace import ws
    path = ws.skill_ref("people-scout", "CRITERIA")
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def _load_seen() -> set[str]:
    from kronos.workspace import ws
    path = ws.skill_ref("people-scout", "SEEN")
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8")
    return set(re.findall(r"https?://(?:www\.)?linkedin\.com/in/[\w-]+/?", text))


def _save_seen(new_urls: list[str]) -> None:
    from kronos.workspace import ws
    path = ws.skill_ref("people-scout", "SEEN")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for url in new_urls:
            f.write(f"\n- {url}")


async def run_people_scout() -> None:
    """Discover interesting LinkedIn profiles. Kronos only."""
    if settings.agent_name != "kronos":
        return

    criteria = _load_criteria()
    seen = _load_seen()

    # Rotate focus
    week_num = datetime.now(UTC).isocalendar()[1]
    focus = FOCUS_ROTATION[week_num % len(FOCUS_ROTATION)]

    seen_text = ""
    if seen:
        seen_text = f"\n\nAlready seen ({len(seen)} profiles) — DO NOT include:\n"
        seen_text += "\n".join(list(seen)[:50])

    prompt = f"""Ты — People Scout. Найди 5-10 интересных профессионалов.

Фокус недели: {focus}

Критерии отбора:
{criteria if criteria else 'Interesting people in tech, startups, AI'}
{seen_text}

Для каждого профиля:
- Имя
- LinkedIn URL (если знаешь)
- Что делает (1 предложение)
- Почему интересен (1 предложение)
- Score: 1-10

Формат: HTML (<b>, <a href>)
Русский язык для описаний.

<b>🔍 People Scout — {focus}</b>

1. <b>Name</b> — описание
   Score: X/10
..."""

    model = get_model(ModelTier.STANDARD)
    from langchain_core.messages import HumanMessage
    response = model.invoke([HumanMessage(content=prompt)])
    reply = response.content if isinstance(response.content, str) else str(response.content)

    if not reply or len(reply) < 100:
        log.warning("Empty scout result")
        return

    # Extract LinkedIn URLs from response
    new_urls = re.findall(r"https?://(?:www\.)?linkedin\.com/in/[\w-]+/?", reply)
    new_urls = [u for u in new_urls if u not in seen]
    if new_urls:
        _save_seen(new_urls)
        log.info("Added %d new LinkedIn profiles to SEEN.md", len(new_urls))

    send_bot_api(reply, parse_mode="HTML", topic_id=TOPIC_DIGEST)
    log.info("People scout completed: %d new profiles", len(new_urls))
