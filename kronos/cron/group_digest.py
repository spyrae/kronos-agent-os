"""Group Digest — daily summary of Telegram groups & channels by category.

Pipeline:
  1. Load sources from GROUPS.md (organized by category)
  2. Fetch messages via Telethon userbot (shared client)
  3. Filter significant messages by engagement
  4. Per-batch summarization (LITE tier) — batches of ~10 sources
  5. Final digest synthesis (STANDARD tier)
  6. Send to Telegram topic
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kronos.config import settings
from kronos.cron.notify import TOPIC_DIGEST, send_bot_api
from kronos.llm import ModelTier, get_model

log = logging.getLogger("kronos.cron.group_digest")

# Telegram-supported HTML tags
_ALLOWED_TAGS = {"b", "i", "u", "s", "a", "code", "pre", "em", "strong"}


def _fix_html(text: str) -> str:
    """Fix common LLM HTML issues for Telegram Bot API.

    - Remove unsupported tags
    - Close unclosed tags
    - Escape unmatched < > outside tags
    """
    import html as html_mod

    # Remove unsupported tags (keep only Telegram-allowed)
    def _strip_tag(m):
        tag = m.group(1).lower().split()[0].strip("/")
        if tag in _ALLOWED_TAGS:
            return m.group(0)
        return ""

    text = re.sub(r"<(/?\w[^>]*)>", _strip_tag, text)

    # Close unclosed <b>, <i>, <u>, <s>, <em>, <strong> tags
    for tag in ("b", "i", "u", "s", "em", "strong"):
        opens = len(re.findall(rf"<{tag}(?:\s|>)", text, re.IGNORECASE))
        closes = len(re.findall(rf"</{tag}>", text, re.IGNORECASE))
        for _ in range(opens - closes):
            text += f"</{tag}>"

    # Fix <a> tags: close unclosed
    opens = len(re.findall(r"<a\s", text, re.IGNORECASE))
    closes = len(re.findall(r"</a>", text, re.IGNORECASE))
    for _ in range(opens - closes):
        text += "</a>"

    return text


LOOKBACK_HOURS = 24
MAX_MESSAGES_PER_SOURCE = 200
MIN_MESSAGE_LENGTH = 30
BATCH_SIZE = 10  # sources per LLM summarization call
FETCH_DELAY = 1.5  # seconds between Telethon requests (avoid flood wait)


# ---------------------------------------------------------------------------
# Config: load categories & sources from GROUPS.md
# ---------------------------------------------------------------------------

def _load_groups() -> dict[str, list[dict]]:
    """Load monitored sources from GROUPS.md, organized by category.

    Returns dict: category_name -> list of source dicts.
    File format: H2 headers = categories, markdown tables = sources.
    Sources can be channels (broadcast) or groups (chats) — both work.
    """
    from kronos.workspace import ws
    path = ws.skill_ref("group-digest", "GROUPS")
    if not path.exists():
        return {}

    text = path.read_text(encoding="utf-8")
    categories: dict[str, list[dict]] = {}
    current_category: str | None = None

    for line in text.splitlines():
        # Detect H2 category headers: ## Category Name
        h2_match = re.match(r"^##\s+(.+)$", line.strip())
        if h2_match:
            current_category = h2_match.group(1).strip()
            categories[current_category] = []
            continue

        if current_category is None:
            continue

        # Parse table rows: | Name | @id or id:NNN | Description |
        parts = [p.strip() for p in line.split("|")]
        parts = [p for p in parts if p]
        if len(parts) < 3:
            continue

        name = parts[0]
        identifier = parts[1]

        # Accept @username or id:NNNNN format
        if not identifier.startswith("@") and not identifier.startswith("id:"):
            continue

        categories[current_category].append({
            "name": name,
            "identifier": identifier,
            "description": parts[2],
        })

    # Remove empty categories
    return {cat: groups for cat, groups in categories.items() if groups}


# ---------------------------------------------------------------------------
# Fetch: Telethon message retrieval
# ---------------------------------------------------------------------------

async def _fetch_messages(
    source_id: str, hours: int = LOOKBACK_HOURS
) -> list[dict]:
    """Fetch recent messages from a Telegram group or channel via userbot.

    Works for both broadcast channels and supergroups/chats.
    """
    from kronos.telegram_client import get_userbot

    client = await get_userbot()
    if not client:
        log.warning("Userbot not available — run scripts/auth-userbot.py first")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    messages = []

    try:
        # Resolve source: @username or id:NNNNN
        if source_id.startswith("id:"):
            numeric_id = int(source_id[3:])
            entity = await client.get_entity(numeric_id)
        else:
            entity = await client.get_entity(source_id)

        # Build permalink base: t.me/username/ID or t.me/c/channel_id/ID
        username = getattr(entity, "username", None)
        entity_id = getattr(entity, "id", None)
        if username:
            permalink_base = f"https://t.me/{username}"
        elif entity_id:
            permalink_base = f"https://t.me/c/{entity_id}"
        else:
            permalink_base = None

        async for msg in client.iter_messages(entity, limit=MAX_MESSAGES_PER_SOURCE):
            if msg.date and msg.date < cutoff:
                break

            if not msg.text or len(msg.text) < MIN_MESSAGE_LENGTH:
                continue

            # Reaction count (works for both channels and groups)
            reactions = 0
            if msg.reactions and msg.reactions.results:
                reactions = sum(r.count for r in msg.reactions.results)

            views = msg.views or 0

            # Sender: for channels it's the channel itself, for groups it's the user
            sender_name = ""
            if msg.sender:
                sender_name = (
                    getattr(msg.sender, "first_name", "")
                    or getattr(msg.sender, "title", "")
                    or ""
                )

            # Extract URLs from message text
            urls = re.findall(r"https?://\S+", msg.text)

            # Permalink to this specific post
            post_link = f"{permalink_base}/{msg.id}" if permalink_base else ""

            messages.append({
                "text": msg.text[:500],
                "author": sender_name,
                "reactions": reactions,
                "views": views,
                "date": msg.date.strftime("%H:%M"),
                "urls": urls[:3],
                "post_link": post_link,
            })

    except Exception as e:
        log.error("Failed to fetch '%s': %s", source_id, e)

    return messages


# ---------------------------------------------------------------------------
# Filter: engagement-based significance
# ---------------------------------------------------------------------------

def _filter_significant(
    messages: list[dict],
    min_reactions: int = 3,
    min_views: int = 200,
) -> list[dict]:
    """Filter and rank messages by engagement score."""
    scored = []
    for msg in messages:
        score = msg["reactions"] * 10 + msg["views"] / 100
        msg["_score"] = score
        scored.append(msg)

    scored.sort(key=lambda m: m["_score"], reverse=True)

    significant = [
        m
        for m in scored
        if m["reactions"] >= min_reactions or m["views"] >= min_views
    ]

    # If too few passed filter, take top by score
    if len(significant) < 5 and scored:
        significant = scored[:10]

    return significant[:20]


# ---------------------------------------------------------------------------
# Summarize: batched two-phase LLM pipeline
# ---------------------------------------------------------------------------

async def _summarize_batch(
    category: str, batch: list[dict], batch_num: int
) -> str | None:
    """Summarize a batch of sources within a category (LITE tier).

    Each batch contains ~10 sources to stay within context limits.
    """
    if not batch:
        return None

    groups_text = "\n\n".join(
        f"**{g['name']}** ({g['count']} постов):\n{g['messages_text']}"
        for g in batch
    )

    prompt = f"""Суммаризируй значимые посты из Telegram-каналов и чатов категории "{category}" (часть {batch_num}).

{groups_text}

Правила:
- Выдели 5-10 ключевых тем/новостей
- Для каждой: суть в 1-2 предложениях + источник (канал/чат)
- ОБЯЗАТЕЛЬНО сохраняй ссылки: на инструменты, статьи, репозитории, вакансии
- Ссылки на сами посты [пост: url] тоже сохраняй — они пригодятся в итоговом дайджесте
- Объединяй одну и ту же новость из разных источников
- Отсеивай рекламу, вопросы новичков, дублирующийся контент
- Русский язык, формат plain text (не HTML)
- Будь конкретным: цифры, названия, факты"""

    model = get_model(ModelTier.LITE)
    from langchain_core.messages import HumanMessage

    try:
        response = model.invoke([HumanMessage(content=prompt)])
        content = (
            response.content
            if isinstance(response.content, str)
            else str(response.content)
        )
        if content and len(content) > 30:
            return content
    except Exception as e:
        log.error("Batch summarization failed for '%s' batch %d: %s", category, batch_num, e)

    return None


async def _summarize_category(
    category: str, groups_data: list[dict]
) -> str | None:
    """Summarize all sources in a category, splitting into batches if needed."""
    if not groups_data:
        return None

    # Split into batches
    batches = [
        groups_data[i : i + BATCH_SIZE]
        for i in range(0, len(groups_data), BATCH_SIZE)
    ]

    if len(batches) == 1:
        return await _summarize_batch(category, batches[0], 1)

    # Multiple batches: summarize each, then merge
    batch_summaries = []
    for i, batch in enumerate(batches, 1):
        summary = await _summarize_batch(category, batch, i)
        if summary:
            batch_summaries.append(summary)

    if not batch_summaries:
        return None

    if len(batch_summaries) == 1:
        return batch_summaries[0]

    # Merge batch summaries into one category summary
    merge_text = "\n\n---\n\n".join(
        f"Часть {i}:\n{s}" for i, s in enumerate(batch_summaries, 1)
    )

    merge_prompt = f"""Объедини несколько частей суммари категории "{category}" в единый обзор.

{merge_text}

Правила:
- Объедини дубликаты (одна новость из разных частей)
- Оставь 7-15 самых значимых тем
- Ранжируй по важности
- Русский язык, plain text"""

    model = get_model(ModelTier.LITE)
    from langchain_core.messages import HumanMessage

    try:
        response = model.invoke([HumanMessage(content=merge_prompt)])
        content = (
            response.content
            if isinstance(response.content, str)
            else str(response.content)
        )
        if content and len(content) > 30:
            return content
    except Exception as e:
        log.error("Category merge failed for '%s': %s", category, e)
        # Fallback: concatenate batch summaries
        return "\n\n".join(batch_summaries)

    return None


async def _synthesize_digest(
    today: str, category_summaries: dict[str, str]
) -> str | None:
    """Phase 2: Synthesize final digest from category summaries (STANDARD tier)."""
    summaries_text = "\n\n".join(
        f"=== {cat} ===\n{summary}"
        for cat, summary in category_summaries.items()
    )

    prompt = f"""Ты — Group Digest агент. Дата: {today}.

Вот суммарии по категориям Telegram-каналов и чатов за последние {LOOKBACK_HOURS}ч:

{summaries_text}

Создай финальный дайджест:
1. Для каждой категории — секция с иконкой
2. AI & LLM: 10-15 пунктов (главная категория, тут больше всего контента)
3. Job Market: 5-7 пунктов (самые интересные вакансии и тренды)
4. Каждый пункт: <b>тема</b> — суть в 1-2 предложениях (источник)
5. К каждому пункту ОБЯЗАТЕЛЬНО добавь ссылку — на инструмент, стат��ю, вакансию или пост в Telegram
6. Формат ссылок: <a href="url">текст</a> или <a href="url">→ пост</a>
7. В конце — 2-3 предложения с инсайтами/трендами дня
8. Формат: HTML (<b>, <i>, <a href>)
9. Русский язык
10. Если контента много — не сокращай, пиши полностью

Иконки категорий:
- AI & LLM → 🤖
- Job Market → 💼

Формат:
<b>📱 Дайджест Telegram — {today}</b>

<b>🤖 AI & LLM</b>
• <b>Тема</b> — суть (<a href="url">источник</a>)
• <b>Новый инструмент</b> — описание (<a href="tool_url">сайт</a> | <a href="post_url">пост</a>)
...

<b>💼 Job Market</b>
• <b>Позиция, компания</b> — условия (<a href="vacancy_url">вакансия</a>)
...

<b>💡 Инсайты дня:</b> ..."""

    model = get_model(ModelTier.STANDARD)
    from langchain_core.messages import HumanMessage

    try:
        response = model.invoke([HumanMessage(content=prompt)])
        content = (
            response.content
            if isinstance(response.content, str)
            else str(response.content)
        )
        if content and len(content) > 50:
            return content
    except Exception as e:
        log.error("Digest synthesis failed: %s", e)

    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_group_digest() -> None:
    """Generate daily digest from Telegram groups and channels. Kronos only."""
    if settings.agent_name != "kronos":
        return

    categories = _load_groups()
    if not categories:
        log.info("No sources configured, skipping group-digest")
        return

    total_sources = sum(len(sources) for sources in categories.values())
    log.info(
        "Group digest: %d categories, %d sources",
        len(categories),
        total_sources,
    )

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    category_summaries: dict[str, str] = {}

    for category, sources in categories.items():
        groups_data = []

        for source in sources:
            messages = await _fetch_messages(source["identifier"])
            if not messages:
                log.debug("No messages from %s", source["identifier"])
                await asyncio.sleep(FETCH_DELAY)
                continue

            significant = _filter_significant(messages)
            if not significant:
                await asyncio.sleep(FETCH_DELAY)
                continue

            msg_lines = []
            for m in significant[:10]:
                line = f"- [{m['date']}] {m['text'][:200]}"
                line += f" (👍{m['reactions']}, 👁{m['views']})"
                # Content URLs (tools, articles, vacancies from the message)
                if m.get("urls"):
                    line += f" Ссылки: {' '.join(m['urls'][:2])}"
                # Permalink to the Telegram post itself
                if m.get("post_link"):
                    line += f" [пост: {m['post_link']}]"
                msg_lines.append(line)
            messages_text = "\n".join(msg_lines)

            groups_data.append({
                "name": source["name"],
                "count": len(significant),
                "messages_text": messages_text,
            })

            await asyncio.sleep(FETCH_DELAY)

        if not groups_data:
            log.debug("No significant messages in category '%s'", category)
            continue

        log.info(
            "Category '%s': %d/%d sources had content",
            category,
            len(groups_data),
            len(sources),
        )

        # Phase 1: per-category summary (with batching)
        summary = await _summarize_category(category, groups_data)
        if summary:
            category_summaries[category] = summary
            log.info(
                "Category '%s' summary: %d chars",
                category,
                len(summary),
            )

    if not category_summaries:
        log.info("No significant messages in any category, skipping digest")
        return

    # Phase 2: synthesize final digest
    digest = await _synthesize_digest(today, category_summaries)

    if not digest:
        log.warning("Failed to synthesize digest, sending raw summaries")
        fallback = f"<b>📱 Дайджест Telegram — {today}</b>\n\n"
        for cat, summary in category_summaries.items():
            fallback += f"<b>{cat}</b>\n{summary}\n\n"
        digest = fallback

    # Fix HTML before sending (LLMs often produce broken markup)
    digest = _fix_html(digest)

    log.info(
        "Group digest complete: %d chars, %d categories",
        len(digest),
        len(category_summaries),
    )
    send_bot_api(digest, parse_mode="HTML", topic_id=TOPIC_DIGEST)
