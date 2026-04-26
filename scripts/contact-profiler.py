#!/usr/bin/env python3
"""Contact Profiler — builds dossiers from personal Telegram chat history.

Fetches message history via bridge, analyzes with DeepSeek API,
saves structured dossier to workspace/contacts/<handle>.md.

Usage:
    contact-profiler.py --chat @username [--limit 300] [--dry-run] [--no-notify]

Environment:
    WORKSPACE           Workspace dir (default: /opt/kronos-ii/app/workspace)
    BRIDGE_URL          Bridge HTTP URL (default: http://127.0.0.1:8788)
    WEBHOOK_SECRET      Auth secret for bridge
    DEEPSEEK_API_KEY    DeepSeek API key
    TG_BOT_TOKEN        Telegram bot token (for notifications)
    PROFILER_CHAT_ID    Telegram chat for notifications
    PROFILER_TOPIC_ID   Telegram topic for notifications
    PROFILER_LOG        Log file (default: /var/log/kronos-ii/contact-profiler.log)
"""

import argparse
import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Make kronos package importable from the app root
sys.path.insert(0, str(Path(__file__).parent.parent))
from kronos.security.sanitize import sanitize_text, detect_injection, wrap_untrusted

# --- Config ---

WORKSPACE = Path(os.environ.get("WORKSPACE", "/opt/kronos-ii/workspace"))
CONTACTS_DIR = WORKSPACE / "notes" / "world" / "contacts"
BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8788")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
CHAT_ID = int(os.environ.get("PROFILER_CHAT_ID", "0"))
TOPIC_ID = int(os.environ.get("PROFILER_TOPIC_ID", "0"))
LOG_FILE = os.environ.get("PROFILER_LOG", "/var/log/kronos-ii/contact-profiler.log")

DEEPSEEK_TIMEOUT = 180  # seconds for LLM analysis
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"

# --- Logging ---

log_dir = Path(LOG_FILE).parent
log_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("contact-profiler")


# --- LLM ---

def ask_deepseek(prompt: str, timeout: int = DEEPSEEK_TIMEOUT) -> str:
    """Call DeepSeek chat completions API. Stdlib only (urllib)."""
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")

    payload = json.dumps({
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4000,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{DEEPSEEK_BASE_URL}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        },
    )

    resp = urllib.request.urlopen(req, timeout=timeout)
    data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


# --- Bridge communication ---

def fetch_history(chat: str, limit: int = 300) -> dict:
    """Fetch chat history from bridge."""
    params = urllib.parse.urlencode({"chat": chat, "limit": limit})
    url = f"{BRIDGE_URL}/history?{params}"

    req = urllib.request.Request(
        url,
        headers={"X-Webhook-Secret": WEBHOOK_SECRET},
    )

    log.info("Fetching history: chat=%s, limit=%d", chat, limit)
    resp = urllib.request.urlopen(req, timeout=30)
    data = json.loads(resp.read())
    log.info("Fetched %d messages for %s", data.get("total", 0), chat)
    return data


# --- Message sampling ---

def sample_messages(messages: list[dict], max_count: int = 300) -> list[dict]:
    """Smart sampling for large chats.

    Strategy:
    - Last 150 messages (fresh context)
    - 100 evenly from the middle (patterns)
    - First 50 (beginning of relationship)
    """
    if len(messages) <= max_count:
        return messages

    # Messages are in reverse chronological order (newest first)
    recent = messages[:150]
    oldest = messages[-50:]

    middle_pool = messages[150:-50]
    if middle_pool and len(middle_pool) > 100:
        step = len(middle_pool) // 100
        middle = middle_pool[::step][:100]
    else:
        middle = middle_pool

    # Combine and deduplicate by id, preserving order
    seen_ids = set()
    result = []
    for msg in recent + middle + oldest:
        if msg["id"] not in seen_ids:
            seen_ids.add(msg["id"])
            result.append(msg)

    return result


# --- Prompt ---

def build_prompt(chat_meta: dict, messages: list[dict]) -> str:
    """Build analysis prompt from chat metadata and messages."""
    name = chat_meta.get("first_name") or chat_meta.get("username") or "Unknown"
    username = chat_meta.get("username") or ""
    last_name = chat_meta.get("last_name") or ""
    full_name = f"{name} {last_name}".strip()

    # Format and sanitize messages
    lines = []
    injection_warnings = []
    for msg in reversed(messages):  # chronological order
        date = msg["date"][:10]
        sender = "Я" if msg["is_outgoing"] else full_name
        text = sanitize_text(msg["text"])
        # Check incoming messages for injection attempts
        if not msg["is_outgoing"]:
            injections = detect_injection(text)
            if injections:
                injection_warnings.append(f"  msg {msg['id']}: {injections}")
        lines.append(f"[{date}] {sender}: {text}")

    if injection_warnings:
        log.warning("Potential prompt injection detected in %d messages:\n%s",
                     len(injection_warnings), "\n".join(injection_warnings))

    conversation = "\n".join(lines)

    return f"""Проанализируй переписку в личном Telegram с контактом и составь подробное досье.

## Контакт
Имя: {full_name}
Username: @{username}
Сообщений в выборке: {len(messages)}

## Переписка

{wrap_untrusted(conversation, label="telegram chat history")}

## Задача

Составь структурированное досье из 8 разделов. Каждый вывод подкрепляй цитатами из переписки.

### Формат ответа (строго markdown):

# Досье: {full_name} (@{username})
*Дата: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}*
*Сообщений проанализировано: {len(messages)}*

## 1. Кто этот человек
Роль, сфера деятельности, контекст знакомства. Что известно о профессии, компании, проектах.

## 2. Коммуникационный стиль
Формальность/неформальность, темп ответов, кто чаще инициирует, длина сообщений, эмоциональность.

## 3. Ключевые темы
О чём чаще всего общаемся. Повторяющиеся темы, интересы, области обсуждений.

## 4. Динамика отношений
Кто инициирует общение, баланс give/take, эволюция тона со временем.

## 5. Заметные запросы и предложения
Что просил, что предлагал, о чём договаривались, невыполненные обещания.

## 6. Потенциальная ценность
Профессиональная: экспертиза, связи, ресурсы.
Личная: поддержка, совместные интересы, синергия.

## 7. Психопрофиль
Гипотеза MBTI с обоснованием через конкретные цитаты. Ключевые черты характера.

## 8. Точки входа
Как лучше обращаться: предпочтительные темы, время, стиль коммуникации для максимального отклика.

---

ПРАВИЛА:
- Каждый раздел должен содержать минимум 1-2 цитаты как доказательства
- Цитаты оформляй как: > "текст цитаты" — дата
- Если данных для раздела недостаточно — так и напиши, не выдумывай
- Ответ на русском языке
- Имена, компании, технические термины — на оригинальном языке"""


# --- Save ---

def save_dossier(handle: str, content: str) -> Path:
    """Save dossier to workspace/contacts/<handle>.md."""
    CONTACTS_DIR.mkdir(parents=True, exist_ok=True)

    # Normalize handle: remove @ prefix for filename
    filename = handle.lstrip("@")
    filepath = CONTACTS_DIR / f"{filename}.md"
    filepath.write_text(content, encoding="utf-8")
    log.info("Dossier saved: %s", filepath)
    return filepath


# --- Telegram notification ---

def md_to_html(text: str) -> str:
    """Convert markdown to Telegram HTML (simplified)."""
    import html as _html
    lines = text.split("\n")
    out = []
    for line in lines:
        line = _html.escape(line.strip())
        header_match = re.match(r"^(#{1,4})\s+(.+)$", line)
        if header_match:
            title = header_match.group(2)
            title = re.sub(r"\*\*(.+?)\*\*", r"\1", title)
            out.append(f"\n<b>{title}</b>")
            continue
        if re.match(r"^-{3,}$", line):
            out.append("")
            continue
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", line)
        line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
        out.append(line)
    return "\n".join(out).strip()


def send_telegram(summary: str) -> None:
    """Send short notification to Telegram."""
    if not BOT_TOKEN:
        log.warning("TG_BOT_TOKEN not set, skipping Telegram notification")
        return

    html_text = md_to_html(summary)
    chunks = [html_text[i:i + 4000] for i in range(0, len(html_text), 4000)]

    for chunk in chunks:
        msg = {
            "chat_id": CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "message_thread_id": TOPIC_ID,
        }
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = json.dumps(msg).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            result = json.loads(resp.read())
            if not result.get("ok"):
                log.error("Telegram API error: %s", result)
        except Exception as e:
            log.error("Failed to send Telegram: %s", e)


# --- Main ---

def run_profiler(chat: str, limit: int, dry_run: bool, no_notify: bool) -> None:
    log.info("Contact Profiler started: chat=%s, limit=%d, dry_run=%s", chat, limit, dry_run)

    # 1. Fetch history
    try:
        data = fetch_history(chat, limit=limit)
    except Exception as e:
        log.error("Failed to fetch history: %s", e)
        sys.exit(1)

    messages = data.get("messages", [])
    chat_meta = data.get("chat", {})

    if not messages:
        log.error("No messages found for %s", chat)
        sys.exit(1)

    log.info("Total messages: %d", len(messages))

    # 2. Sample if needed
    sampled = sample_messages(messages, max_count=300)
    log.info("Sampled: %d messages", len(sampled))

    # 3. Build prompt
    prompt = build_prompt(chat_meta, sampled)

    if dry_run:
        log.info("DRY RUN — prompt (%d chars):\n%s", len(prompt), prompt[:2000])
        log.info("... [truncated, total %d chars]", len(prompt))
        return

    # 4. Analyze with DeepSeek
    log.info("Sending to DeepSeek for analysis (%d chars)...", len(prompt))
    try:
        dossier = ask_deepseek(prompt, timeout=DEEPSEEK_TIMEOUT)
    except Exception as e:
        log.error("DeepSeek analysis failed: %s", e)
        sys.exit(1)

    if not dossier:
        log.error("Empty response from DeepSeek")
        sys.exit(1)

    log.info("Dossier generated: %d chars", len(dossier))

    # 5. Save
    handle = chat_meta.get("username") or chat.lstrip("@")
    filepath = save_dossier(handle, dossier)

    # 6. Notify
    if not no_notify:
        name = chat_meta.get("first_name") or handle
        summary = (
            f"**Contact Profiler** — досье готово\n\n"
            f"Контакт: {name} (@{handle})\n"
            f"Сообщений: {len(sampled)}\n"
            f"Файл: `{filepath}`"
        )
        send_telegram(summary)

    log.info("Contact Profiler completed for %s", chat)


def main() -> None:
    parser = argparse.ArgumentParser(description="Contact Profiler — Telegram chat analysis")
    parser.add_argument("--chat", required=True, help="Telegram username or ID (e.g. @ivan)")
    parser.add_argument("--limit", type=int, default=300, help="Max messages to fetch (default: 300)")
    parser.add_argument("--dry-run", action="store_true", help="Build prompt but don't call LLM")
    parser.add_argument("--no-notify", action="store_true", help="Skip Telegram notification")
    args = parser.parse_args()

    try:
        run_profiler(
            chat=args.chat,
            limit=args.limit,
            dry_run=args.dry_run,
            no_notify=args.no_notify,
        )
    except Exception:
        log.exception("Contact Profiler failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
