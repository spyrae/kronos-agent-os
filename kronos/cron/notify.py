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


def _bot_api_group_chat_id(chat_id: int) -> int:
    """Convert t.me/c internal supergroup id to Bot API chat id."""
    if chat_id > 0 and chat_id >= 1_000_000_000:
        return int(f"-100{chat_id}")
    return chat_id


DEFAULT_CHAT = int(os.environ.get("DEFAULT_NOTIFY_CHAT") or "0")
if not DEFAULT_CHAT and settings.telegram_swarm_chat_id:
    DEFAULT_CHAT = _bot_api_group_chat_id(settings.telegram_swarm_chat_id)


def _resolve_topic_id(
    env_name: str,
    setting_value: int = 0,
    fallback: int = 0,
) -> int:
    """Resolve Telegram topic id from env/settings with optional fallback.

    Treat 0/empty as "not configured" so a new topic can inherit the legacy
    digest topic while env examples still use explicit ``0`` placeholders.
    """
    candidates = (os.environ.get(env_name), setting_value, fallback)
    for candidate in candidates:
        try:
            value = int(candidate or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


# Bot chat topic IDs (topics inside the KAOS bot DM)
TOPIC_GENERAL = _resolve_topic_id(
    "TOPIC_GENERAL",
    settings.telegram_general_topic_id,
)
TOPIC_DIGEST = _resolve_topic_id(
    "TOPIC_DIGEST",
    settings.telegram_digest_topic_id,
)
TOPIC_FINANCE = _resolve_topic_id(
    "TOPIC_FINANCE",
    settings.telegram_finance_topic_id,
)

# Signal Intelligence destinations. All default to the legacy digest topic
# until the corresponding Telegram topics are created and configured.
TOPIC_DIGEST_NEWS = _resolve_topic_id(
    "TOPIC_DIGEST_NEWS",
    settings.telegram_digest_news_topic_id,
    TOPIC_DIGEST,
)
TOPIC_JB_COMPETITORS = _resolve_topic_id(
    "TOPIC_JB_COMPETITORS",
    settings.telegram_jb_competitors_topic_id,
    TOPIC_DIGEST,
)
TOPIC_JB_SYSTEM = _resolve_topic_id(
    "TOPIC_JB_SYSTEM",
    settings.telegram_jb_system_topic_id,
    TOPIC_DIGEST,
)
TOPIC_DIGEST_JOBS = _resolve_topic_id(
    "TOPIC_DIGEST_JOBS",
    settings.telegram_digest_jobs_topic_id,
    TOPIC_DIGEST,
)
TOPIC_DIGEST_IDEAS = _resolve_topic_id(
    "TOPIC_DIGEST_IDEAS",
    settings.telegram_digest_ideas_topic_id,
    TOPIC_DIGEST,
)
TOPIC_JB_TRAVEL_INSIGHTS = _resolve_topic_id(
    "TOPIC_JB_TRAVEL_INSIGHTS",
    settings.telegram_jb_travel_insights_topic_id,
    TOPIC_DIGEST,
)


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


def _markdown_to_html(text: str) -> str:
    """Convert common Markdown to Telegram-supported HTML.

    LLMs almost always emit Markdown (**bold**, *italic*, `code`, ### headings)
    even when prompted to write HTML. Telegram's HTML parser shows raw ** etc.
    Translate before sanitize so users see proper formatting.
    """
    # Strip markdown headings (### / ## / #) — Telegram has no equivalent;
    # keep the text and bold it instead.
    text = re.sub(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", r"<b>\1</b>", text, flags=re.MULTILINE)
    # Bold-italic ***text*** -> <b><i>text</i></b>
    text = re.sub(r"\*\*\*([^*\n][^*\n]*?)\*\*\*", r"<b><i>\1</i></b>", text)
    # Bold **text** -> <b>text</b>
    text = re.sub(r"\*\*([^*\n][^*\n]*?)\*\*", r"<b>\1</b>", text)
    # Italic *text* -> <i>text</i> — only if the * is not part of a list bullet
    # (i.e. not at line start followed by space). Allow underscores too.
    text = re.sub(r"(?<![*\w])\*([^*\n][^*\n]*?)\*(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"(?<![_\w])_([^_\n][^_\n]*?)_(?!\w)", r"<i>\1</i>", text)
    # Inline code `text` -> <code>text</code>
    text = re.sub(r"`([^`\n]+?)`", r"<code>\1</code>", text)
    # Markdown links [label](url) -> <a href="url">label</a>
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


_TG_ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "s", "a", "code", "pre"}
_HTML_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)((?:\s[^<>]*)?)>")
_STRAY_AMP_RE = re.compile(r"&(?!amp;|lt;|gt;|quot;|#\d+;|#x[0-9a-fA-F]+;)")


def _escape_html_text(segment: str) -> str:
    """Escape stray HTML specials in a plain-text segment (keeps valid entities)."""
    segment = _STRAY_AMP_RE.sub("&amp;", segment)
    return segment.replace("<", "&lt;").replace(">", "&gt;")


def _telegram_safe_html(text: str) -> str:
    """Return Telegram-valid HTML: escape stray specials, balance allowed tags.

    Text between tags is entity-escaped; unknown tags are escaped to literal
    text; allowed tags are kept but balanced (orphan closers dropped, unclosed
    openers closed) so Telegram's parser never rejects the message and falls
    back to raw-tag plain text.
    """
    out: list[str] = []
    stack: list[str] = []
    pos = 0
    for match in _HTML_TAG_RE.finditer(text):
        out.append(_escape_html_text(text[pos : match.start()]))
        pos = match.end()
        closing, name, attrs = match.group(1), match.group(2).lower(), match.group(3)
        if name not in _TG_ALLOWED_TAGS:
            out.append(_escape_html_text(match.group(0)))
            continue
        if not closing:
            stack.append(name)
            out.append(f"<{name}{attrs}>")
        elif name in stack:
            while stack:
                top = stack.pop()
                out.append(f"</{top}>")
                if top == name:
                    break
        # orphan closing tag with no matching opener → dropped
    out.append(_escape_html_text(text[pos:]))
    while stack:
        out.append(f"</{stack.pop()}>")
    return "".join(out)


def _sanitize_html(text: str) -> str:
    """Strip full HTML documents down to body content for Telegram.

    Also normalises Markdown LLMs commonly emit into Telegram-supported HTML
    (see _markdown_to_html) and guarantees Telegram-valid HTML via
    _telegram_safe_html.
    """
    # LLMs sometimes wrap output in <!DOCTYPE html>...<body>...</body>
    body_match = re.search(r"<body[^>]*>(.*)</body>", text, re.DOTALL | re.IGNORECASE)
    if body_match:
        text = body_match.group(1).strip()
    # Convert Markdown to allowed HTML tags *before* stripping unknown tags
    text = _markdown_to_html(text)
    # Remove unsupported tags (keep only b, i, a, code, pre, u, s, em, strong)
    text = re.sub(r"<!DOCTYPE[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"</?(?:html|head|body|meta|title|div|span|p|br\s*/?|h[1-6]|ul|ol|li|table|tr|td|th|thead|tbody|img|hr)[^>]*>",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # Collapse multiple newlines
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Final pass: escape stray &<> and balance tags so Telegram never rejects
    # the HTML (LLMs occasionally emit an orphan </i> or an unescaped &).
    text = _telegram_safe_html(text)
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
            result.extend(chunk[i : i + max_len] for i in range(0, len(chunk), max_len))

    return result


def send_bot_api(
    text: str,
    chat_id: int | None = None,
    parse_mode: str = "HTML",
    topic_id: int | None = None,
) -> bool:
    """Send message via Telegram Bot API directly (for topic support)."""
    # Sanitize Markdown -> HTML BEFORE the webhook fallback so the local
    # Telethon bridge also receives Telegram-ready content. Otherwise pulse
    # text reaches Telegram with raw "**bold**" markers.
    if parse_mode == "HTML":
        text = _sanitize_html(text)

    token = settings.tg_bot_token
    if not token:
        return send_webhook(text, chat_id, parse_mode, topic_id)

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
