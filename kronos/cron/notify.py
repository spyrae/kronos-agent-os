"""Notification helpers for cron tasks.

Three methods:
1. webhook — POST to bridge webhook (simple, uses same session)
2. bot_api — direct Telegram Bot API (for topic messages)
3. ntfy — push notification to phone via NTFY server
"""

import json
import logging
import os
import re
import urllib.request

from kronos.config import settings

log = logging.getLogger("kronos.cron.notify")

WEBHOOK_URL = "http://127.0.0.1:{port}/webhook"
DEFAULT_CHAT = int(os.environ.get("DEFAULT_NOTIFY_CHAT", "0"))

# Bot chat topic IDs (topics inside the KAOS bot DM)
TOPIC_GENERAL = int(os.environ.get("TOPIC_GENERAL", "0"))
TOPIC_DIGEST = int(os.environ.get("TOPIC_DIGEST", "0"))


def send_webhook(
    text: str,
    chat_id: int | None = None,
    parse_mode: str | None = None,
    topic_id: int | None = None,
) -> bool:
    """Send message via bridge webhook."""
    port = int(os.environ.get("WEBHOOK_PORT", "8788"))
    url = WEBHOOK_URL.format(port=port)

    body: dict = {"text": text}
    if chat_id:
        body["chat_id"] = chat_id
    if parse_mode:
        body["parse_mode"] = parse_mode
    if topic_id:
        body["topic_id"] = topic_id

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Secret": settings.webhook_secret,
            },
        )
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.status == 200
    except Exception as e:
        log.error("Webhook send failed: %s", e)
        return False


def _sanitize_html(text: str) -> str:
    """Strip full HTML documents down to body content for Telegram."""
    # LLMs sometimes wrap output in <!DOCTYPE html>...<body>...</body>
    body_match = re.search(r"<body[^>]*>(.*)</body>", text, re.DOTALL | re.IGNORECASE)
    if body_match:
        text = body_match.group(1).strip()
    # Remove unsupported tags (keep only b, i, a, code, pre, u, s, em, strong)
    text = re.sub(r"<!DOCTYPE[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:html|head|body|meta|title|div|span|p|br\s*/?|h[1-6]|ul|ol|li|table|tr|td|th|thead|tbody|img|hr)[^>]*>", "", text, flags=re.IGNORECASE)
    # Collapse multiple newlines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _send_message(url: str, body: dict) -> bool:
    """Send a single message, retry without parse_mode on HTML error."""
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.status == 200
    except urllib.request.HTTPError as e:
        err_body = e.read().decode()
        if e.code == 400 and "can't parse entities" in err_body:
            log.warning("HTML parse failed, retrying as plain text")
            body.pop("parse_mode", None)
            try:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(body).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                resp = urllib.request.urlopen(req, timeout=15)
                return resp.status == 200
            except Exception as e2:
                log.error("Bot API send failed (plain fallback): %s", e2)
                return False
        log.error("Bot API send failed: %s — %s", e, err_body[:200])
        return False
    except Exception as e:
        log.error("Bot API send failed: %s", e)
        return False


def _split_by_lines(text: str, max_len: int = 4000) -> list[str]:
    """Split text into chunks at line boundaries, respecting max_len."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    current = ""
    for line in text.split("\n"):
        # +1 for the newline character
        if current and len(current) + len(line) + 1 > max_len:
            chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line

    if current:
        chunks.append(current)

    # Safety: if any single chunk is still too long, hard-split it
    result = []
    for chunk in chunks:
        if len(chunk) <= max_len:
            result.append(chunk)
        else:
            result.extend(chunk[i:i + max_len] for i in range(0, len(chunk), max_len))

    return result


def send_bot_api(
    text: str,
    chat_id: int | None = None,
    parse_mode: str = "HTML",
    topic_id: int | None = None,
) -> bool:
    """Send message via Telegram Bot API directly (for topic support)."""
    token = settings.tg_bot_token
    if not token:
        return send_webhook(text, chat_id, parse_mode, topic_id)

    if parse_mode == "HTML":
        text = _sanitize_html(text)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body: dict = {
        "chat_id": chat_id or DEFAULT_CHAT,
        "text": text,
    }
    if parse_mode:
        body["parse_mode"] = parse_mode
    if topic_id:
        body["message_thread_id"] = topic_id

    # Chunk long messages — split by lines, not mid-sentence
    if len(text) > 4000:
        chunks = _split_by_lines(text, max_len=4000)
        for chunk in chunks:
            body["text"] = chunk
            if not _send_message(url, body.copy()):
                return False
        return True

    return _send_message(url, body)


def send_ntfy(
    text: str,
    title: str = "Kronos Agent OS",
    priority: str = "default",
    tags: str = "robot_face",
) -> bool:
    """Send push notification via NTFY server (phone alerts)."""
    if not settings.ntfy_token:
        log.debug("NTFY_TOKEN not set, skipping push notification")
        return False

    url = f"{settings.ntfy_url}/{settings.ntfy_topic}"

    try:
        req = urllib.request.Request(
            url,
            data=text.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority,
                "Tags": tags,
                "Authorization": f"Bearer {settings.ntfy_token}",
            },
        )
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.status == 200
    except Exception as e:
        log.error("NTFY send failed: %s", e)
        return False


def alert(text: str, title: str = "Kronos Agent OS Alert") -> None:
    """Send alert to both Telegram and NTFY (for critical notifications)."""
    send_webhook(text)
    send_ntfy(text, title=title, priority="urgent", tags="rotating_light,skull")
