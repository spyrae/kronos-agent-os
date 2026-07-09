"""Telegram bridge — adapted from Kronos I bridge.py.

Telethon userbot + webhook server for cron scripts.
Calls KronosAgent for message processing.
"""

import asyncio
import logging
import os
import random
import secrets
import tempfile
import time
from dataclasses import dataclass

import aiohttp
from aiohttp import web
from telethon import Button, TelegramClient, events
from telethon.tl.types import DocumentAttributeAudio

from kronos.audit import log_request
from kronos.config import settings
from kronos.graph import KronosAgent
from kronos.observer.capture import CaptureDecision, classify_capture, record_capture
from kronos.security.cost_guardian import get_guardian
from kronos.security.output_validator import validate_output
from kronos.swarm_store import get_swarm
from kronos.tts import get_voice_mode, set_voice_mode, should_synthesize, synthesize
from kronos.vision import analyze_image_bytes, is_supported_image_mime, is_vision_configured

log = logging.getLogger("kronos.bridge")

# Rate limiting state
RATE_LIMIT_MIN_DELAY = 2.0
RATE_LIMIT_GLOBAL_DELAY = 1.0
_last_send_per_chat: dict[int, float] = {}
_last_send_global: float = 0.0
_rate_lock = asyncio.Lock()

# Groq Whisper STT
GROQ_WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_WHISPER_MODEL = "whisper-large-v3-turbo"

# Default chat for cron notifications
DEFAULT_NOTIFY_CHAT = int(os.environ.get("DEFAULT_NOTIFY_CHAT") or "0")
if not DEFAULT_NOTIFY_CHAT and settings.telegram_swarm_chat_id:
    DEFAULT_NOTIFY_CHAT = (
        int(f"-100{settings.telegram_swarm_chat_id}")
        if settings.telegram_swarm_chat_id > 0
        else settings.telegram_swarm_chat_id
    )

# Per-thread serialization + bounded global concurrency. A single heavy ReAct
# cycle (up to 25 turns × 120s tool timeout) must not block every other
# chat/topic/DM/peer-reaction. Each thread_id runs its turns in order (history
# consistency); the semaphore caps how many threads hit the LLM API at once.
_agent_semaphore = asyncio.Semaphore(int(os.environ.get("AGENT_MAX_CONCURRENCY", "3")))
_thread_locks: dict[str, asyncio.Lock] = {}


def _thread_lock(thread_id: str) -> asyncio.Lock:
    """Return the per-thread lock, creating it on first use.

    Synchronous on purpose: with no await between the get and the set, the
    asyncio event loop can't interleave two calls, so there's no race and no
    guard lock is needed. thread_id space is bounded by active chats×topics
    (tens for a personal swarm), so the dict is never evicted.
    """
    lock = _thread_locks.get(thread_id)
    if lock is None:
        lock = asyncio.Lock()
        _thread_locks[thread_id] = lock
    return lock

# Agent reference (set in run_bridge)
_agent: KronosAgent | None = None
_client: TelegramClient | None = None
_my_id: int | None = None
# Monotonic-wall timestamp of the last incoming Telegram event, for /health
# liveness (0.0 = nothing received yet).
_last_event_ts: float = 0.0


def get_agent() -> "KronosAgent | None":
    """Current KronosAgent instance (set in run_bridge), or None.

    Lets in-process cron jobs (e.g. follow-ups) reuse the live agent instead of
    building a second one.
    """
    return _agent
_my_username: str | None = None

# Group routing (initialized in run_bridge after login)
_group_router = None  # GroupRouter | None

APPROVAL_CALLBACK_PREFIX = "kaos:approval:"


@dataclass(frozen=True)
class TopicRoute:
    """Routing mode for a configured Telegram forum topic."""

    mode: str  # default | swarm | owner | silent
    label: str = ""
    owner_agent: str = ""


@dataclass(frozen=True)
class TopicDecision:
    """Small decision object compatible with group_router.RoutingDecision."""

    should_respond: bool
    delay: float
    tier: int
    reason: str
    addressing: object | None = None


def _normalize_telegram_chat_id(chat_id: int | None) -> int | None:
    """Normalize Telegram supergroup ids for matching.

    Telegram topic links use the internal id (3642435967), while Bot API and
    Telethon commonly expose the same supergroup as -1003642435967.
    """
    if chat_id is None:
        return None
    normalized = abs(int(chat_id))
    if normalized > 1_000_000_000_000 and str(normalized).startswith("100"):
        normalized -= 1_000_000_000_000
    return normalized


def _same_telegram_chat(left: int | None, right: int | None) -> bool:
    if not left or not right:
        return False
    return _normalize_telegram_chat_id(left) == _normalize_telegram_chat_id(right)


def _positive_int(value: object) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _topic_id_from_env_or_setting(env_name: str, setting_value: int) -> int:
    """Resolve a topic id for inbound bridge routing.

    Cron notifications historically use ``TOPIC_*`` env aliases, while the
    bridge settings use ``TELEGRAM_*_TOPIC_ID``. For owner-topic safety the
    bridge must honor both forms; otherwise a configured cron topic could still
    fall through to generic group routing for inbound user messages.
    """
    return _positive_int(os.environ.get(env_name)) or _positive_int(setting_value)


def _topic_owner_agents(owner_agent: str) -> set[str]:
    """Return normalized allowed owners for a topic.

    Supports comma-separated values such as ``kronos,nexus`` for topics where
    both agents are allowed to answer.
    """
    return {agent.strip().lower() for agent in (owner_agent or "").replace(";", ",").split(",") if agent.strip()}


def _approval_callback_data(action: str, approval_id: str) -> bytes:
    """Build compact callback payload for Telegram inline buttons."""
    return f"{APPROVAL_CALLBACK_PREFIX}{action}:{approval_id}".encode()


def _parse_approval_callback_data(data: bytes | str) -> tuple[str, str] | None:
    """Parse approval callback payload into (action, approval_id)."""
    text = data.decode("utf-8") if isinstance(data, bytes) else str(data)
    if not text.startswith(APPROVAL_CALLBACK_PREFIX):
        return None
    remainder = text[len(APPROVAL_CALLBACK_PREFIX) :]
    try:
        action, approval_id = remainder.split(":", 1)
    except ValueError:
        return None
    if action not in {"approve", "reject"} or not approval_id:
        return None
    return action, approval_id


def _approval_buttons(approval_id: str):
    """Return Telethon inline buttons for a pending approval."""
    return [
        [
            Button.inline("✅ Approve", _approval_callback_data("approve", approval_id)),
            Button.inline("❌ Reject", _approval_callback_data("reject", approval_id)),
        ]
    ]


def _approval_bot_reply_markup(approval_id: str) -> dict:
    """Return Bot API inline_keyboard markup for topic sends."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Approve",
                    "callback_data": _approval_callback_data(
                        "approve",
                        approval_id,
                    ).decode("utf-8"),
                },
                {
                    "text": "❌ Reject",
                    "callback_data": _approval_callback_data(
                        "reject",
                        approval_id,
                    ).decode("utf-8"),
                },
            ]
        ],
    }


def _last_pending_approval_id() -> str | None:
    """Return latest pending approval from the active agent, if any."""
    if _agent is None:
        return None
    return getattr(_agent, "last_pending_approval_id", None)


def _chat_topic_from_thread_id(thread_id: str) -> tuple[int | None, int | None]:
    """Parse a Telegram thread id into chat/topic ids when possible."""
    try:
        chat_text, topic_text = str(thread_id).rsplit(":", 1)
        return int(chat_text), int(topic_text)
    except (TypeError, ValueError):
        try:
            return int(str(thread_id)), None
        except (TypeError, ValueError):
            return None, None


def _resolve_topic_route(chat_id: int, topic_id: int | None) -> TopicRoute:
    """Return how this process should treat a group/topic message."""
    if not settings.telegram_swarm_chat_id:
        return TopicRoute("default")
    if not _same_telegram_chat(chat_id, settings.telegram_swarm_chat_id):
        return TopicRoute("default")

    topic = topic_id or 0
    general_topic = _topic_id_from_env_or_setting(
        "TOPIC_GENERAL",
        settings.telegram_general_topic_id,
    )

    if general_topic and topic == general_topic:
        return TopicRoute("swarm", label="general")
    if not general_topic and topic == 0:
        return TopicRoute("swarm", label="general")

    owner_topics = (
        (
            _topic_id_from_env_or_setting(
                "TELEGRAM_KRONOS_TOPIC_ID",
                settings.telegram_kronos_topic_id,
            ),
            settings.telegram_kronos_agent,
            "kronos",
        ),
        (
            _topic_id_from_env_or_setting(
                "TOPIC_FINANCE",
                settings.telegram_finance_topic_id,
            ),
            settings.telegram_finance_agent,
            "finance",
        ),
        (
            _topic_id_from_env_or_setting(
                "TOPIC_DIGEST_NEWS",
                settings.telegram_digest_news_topic_id,
            ),
            settings.telegram_digest_news_agent,
            "digest_news",
        ),
        (
            _topic_id_from_env_or_setting(
                "TOPIC_JB_COMPETITORS",
                settings.telegram_jb_competitors_topic_id,
            ),
            settings.telegram_jb_competitors_agent,
            "jb_competitors",
        ),
        (
            _topic_id_from_env_or_setting(
                "TOPIC_JB_SYSTEM",
                settings.telegram_jb_system_topic_id,
            ),
            settings.telegram_jb_system_agent,
            "jb_system",
        ),
        (
            _topic_id_from_env_or_setting(
                "TOPIC_DIGEST_JOBS",
                settings.telegram_digest_jobs_topic_id,
            ),
            settings.telegram_digest_jobs_agent,
            "digest_jobs",
        ),
        (
            _topic_id_from_env_or_setting(
                "TOPIC_DIGEST_IDEAS",
                settings.telegram_digest_ideas_topic_id,
            ),
            settings.telegram_digest_ideas_agent,
            "digest_ideas",
        ),
        (
            _topic_id_from_env_or_setting(
                "TOPIC_JB_TRAVEL_INSIGHTS",
                settings.telegram_jb_travel_insights_topic_id,
            ),
            settings.telegram_jb_travel_insights_agent,
            "jb_travel_insights",
        ),
        (
            _topic_id_from_env_or_setting(
                "TOPIC_DIGEST",
                settings.telegram_digest_topic_id,
            ),
            settings.telegram_digest_agent,
            "digest",
        ),
    )
    for configured_topic, owner_agent, label in owner_topics:
        if configured_topic and topic == configured_topic:
            return TopicRoute("owner", label=label, owner_agent=(owner_agent or "").lower())

    return TopicRoute("silent", label=f"unconfigured:{topic}")


def _agent_owns_topic(route: TopicRoute) -> bool:
    return settings.agent_name.lower() in _topic_owner_agents(route.owner_agent)


def _owner_topic_accepts_sender(user_id: int) -> bool:
    """Owner topics are direct user->owner channels, not peer debate rooms."""
    if _group_router is not None:
        return not _group_router._is_peer(user_id)
    return settings.is_telegram_user_allowed(user_id)


def _clip_context_text(text: str, limit: int = 500) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)].rstrip() + "..."


def _format_shared_group_context(
    swarm,
    *,
    chat_id: int,
    topic_id: int | None,
    current_msg_id: int | None,
) -> str:
    """Build transient context from the shared swarm ledger."""
    limit = max(0, min(settings.telegram_shared_context_messages, 30))
    if limit <= 0:
        return ""

    try:
        rows = swarm.get_recent_messages(chat_id=chat_id, topic_id=topic_id, limit=limit + 1)
    except Exception as e:
        log.warning("[Swarm] Failed to load shared topic context: %s", e)
        return ""

    rows = [row for row in rows if row.get("msg_id") != current_msg_id]
    if not rows:
        return ""

    lines: list[str] = []
    for row in reversed(rows[:limit]):
        sender_type = row.get("sender_type")
        if sender_type == "agent":
            who = f"Агент {row.get('agent_name') or 'unknown'}"
        elif sender_type == "system":
            who = "Система"
        else:
            who = "Пользователь"
        lines.append(f"- {who}: {_clip_context_text(str(row.get('text') or ''))}")

    if not lines:
        return ""
    return (
        "[Общая история этого Telegram-топика]\n"
        "Ниже недавние сообщения из общего журнала. Используй их как контекст, "
        "но не считай новым запросом и не пересказывай без необходимости.\n" + "\n".join(lines)
    )


async def _rate_limit_wait(chat_id: int) -> None:
    """Enforce anti-spam delays before sending."""
    global _last_send_global
    async with _rate_lock:
        now = time.monotonic()
        last_chat = _last_send_per_chat.get(chat_id, 0.0)
        chat_wait = max(0.0, RATE_LIMIT_MIN_DELAY - (now - last_chat))
        global_wait = max(0.0, RATE_LIMIT_GLOBAL_DELAY - (now - _last_send_global))
        wait = max(chat_wait, global_wait)
        if wait > 0:
            await asyncio.sleep(wait)
        now = time.monotonic()
        _last_send_per_chat[chat_id] = now
        _last_send_global = now
        if len(_last_send_per_chat) > 200:
            _last_send_per_chat.clear()


async def _human_typing_delay(chat_id: int, text: str) -> None:
    """Simulate human typing speed."""
    chars = len(text)
    typing_secs = chars / random.uniform(40, 80)
    thinking_secs = random.uniform(0.3, 1.2)
    total = min(typing_secs + thinking_secs, 5.0)
    async with _client.action(chat_id, "typing"):
        await asyncio.sleep(total)


def _is_voice_message(event) -> bool:
    if not event.message.media:
        return False
    doc = getattr(event.message.media, "document", None)
    if not doc:
        return False
    return any(isinstance(attr, DocumentAttributeAudio) and attr.voice for attr in doc.attributes)


def _image_mime_type(event) -> str:
    if getattr(event.message, "photo", None):
        return "image/jpeg"
    doc = getattr(getattr(event.message, "media", None), "document", None)
    return str(getattr(doc, "mime_type", "") or "")


def _is_image_message(event) -> bool:
    if not getattr(event.message, "media", None):
        return False
    return is_supported_image_mime(_image_mime_type(event))


async def _download_image_bytes(event) -> tuple[bytes, str]:
    mime_type = _image_mime_type(event) or "image/jpeg"
    suffix = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(mime_type, ".img")
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
        await event.message.download_media(file=tmp_path)
        with open(tmp_path, "rb") as f:
            return f.read(), mime_type
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def _analyze_image_message(event, caption: str) -> str:
    if not is_vision_configured():
        return (
            "Я получил изображение, но vision model не настроена. "
            "Нужно установить и авторизовать Codex CLI (`codex login`) "
            "или включить KAOS_VISION_PROVIDER=openai-api."
        )
    image_bytes, mime_type = await _download_image_bytes(event)
    result = await analyze_image_bytes(
        image_bytes,
        mime_type=mime_type,
        context=caption,
    )
    return result.text


def _compose_image_agent_message(caption: str, image_analysis: str) -> str:
    caption = caption.strip()
    user_request = caption or "Пользователь отправил изображение без подписи."
    return (
        f"{user_request}\n\n"
        "[Vision analysis]\n"
        f"{image_analysis}\n\n"
        "Ответь пользователю на основе анализа изображения. Если пользователь просит OCR, "
        "верни извлечённый текст; если это документ/скриншот/чек, кратко классифицируй "
        "и выдели важные детали/action items."
    )


# --- Document ingest (roadmap 6.1) ---

_DOC_EXTENSIONS = (".pdf", ".docx", ".txt", ".md", ".markdown")


def _document_info(event) -> tuple[str, str]:
    """(filename, mime_type) for a document attachment, or ("", "")."""
    doc = getattr(getattr(event.message, "media", None), "document", None)
    if doc is None:
        return "", ""
    from telethon.tl.types import DocumentAttributeFilename

    filename = ""
    for attr in getattr(doc, "attributes", []):
        if isinstance(attr, DocumentAttributeFilename):
            filename = attr.file_name
            break
    return filename, str(getattr(doc, "mime_type", "") or "")


def _is_document_message(event) -> bool:
    """A non-image, non-voice document we can ingest (by extension or mime)."""
    if not getattr(event.message, "media", None):
        return False
    if _is_image_message(event) or _is_voice_message(event):
        return False
    filename, mime = _document_info(event)
    low = filename.lower()
    return (
        low.endswith(_DOC_EXTENSIONS)
        or "pdf" in mime
        or "wordprocessingml" in mime
        or mime.startswith("text/")
    )


async def _download_document_bytes(event) -> bytes:
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name
        await event.message.download_media(file=tmp_path)
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def _extract_document_message(event) -> tuple[str, str]:
    """Download + extract text; archive raw text to notes/inbox. (text, error)."""
    from kronos.documents.extract import extract_text
    from kronos.workspace import ws

    filename, mime = _document_info(event)
    data = await _download_document_bytes(event)
    text, error = extract_text(data, filename, mime)
    if error:
        return "", error

    # Archive raw text to the inbox for later processing / provenance.
    try:
        ws.inbox_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = "".join(c for c in (filename or "document") if c.isalnum() or c in "-_.") or "document"
        (ws.inbox_dir / f"{stamp}-{safe}.txt").write_text(text, encoding="utf-8")
    except Exception as e:
        log.warning("Failed to archive document to inbox: %s", e)

    return text, ""


def _compose_document_agent_message(caption: str, filename: str, text: str) -> str:
    caption = caption.strip()
    user_request = caption or f"Пользователь прислал документ «{filename}»."
    return (
        f"{user_request}\n\n"
        f"[Документ: {filename}]\n{text}\n\n"
        "Дай краткое саммари документа и выдели ключевые факты и action items."
    )


def _message_timestamp(event) -> str:
    value = getattr(getattr(event, "message", None), "date", None)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value or "")


def _observer_capture_confirmation(decision: CaptureDecision, task: dict) -> str:
    label = {
        "telegram_voice_note": "голосовую заметку",
        "telegram_link": "ссылку",
        "telegram_text_capture": "заметку",
    }.get(decision.source_kind_value, "заметку")
    task_id = str(task.get("task_id") or "без id")
    return f"Сохранил в inbox: {label}. ID: `{task_id}`"


async def _send_observer_capture_reply(event, reply: str) -> None:
    await _rate_limit_wait(event.chat_id)
    await _client.send_message(event.chat_id, reply)


async def _maybe_record_observer_capture(
    event,
    clean_text: str,
    *,
    is_dm: bool,
    voice: bool,
    image: bool,
    user_id: int,
) -> bool:
    """Record explicit DM captures before invoking the agent."""
    if not is_dm:
        return False

    decision = classify_capture(
        clean_text,
        is_voice=voice,
        is_dm=is_dm,
        has_image=image,
    )
    if not decision.should_capture:
        return False

    try:
        task = record_capture(
            clean_text,
            is_voice=voice,
            is_dm=is_dm,
            has_image=image,
            chat_id=event.chat_id,
            user_id=user_id,
            message_id=getattr(event.message, "id", None),
            timestamp=_message_timestamp(event),
        )
    except Exception as e:
        log.error("[ObserverCapture] Failed to record capture: %s", e)
        await _send_observer_capture_reply(
            event,
            "Не удалось сохранить в inbox. Проверь логи Kronos.",
        )
        return True

    if task is None:
        return False

    await _send_observer_capture_reply(
        event,
        _observer_capture_confirmation(decision, task),
    )
    log.info(
        "[ObserverCapture] captured source=%s chat=%s message=%s task=%s",
        decision.source_kind_value,
        event.chat_id,
        getattr(event.message, "id", None),
        task.get("task_id"),
    )
    return True


async def _transcribe_voice(file_path: str) -> str:
    """Transcribe audio via Groq Whisper API."""
    async with aiohttp.ClientSession() as session:
        data = aiohttp.FormData()
        fh = open(file_path, "rb")
        try:
            data.add_field(
                "file",
                fh,
                filename=os.path.basename(file_path),
                content_type="audio/ogg",
            )
            data.add_field("model", GROQ_WHISPER_MODEL)
            async with session.post(
                GROQ_WHISPER_URL,
                headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                data=data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"Groq STT error {resp.status}: {body}")
                result = await resp.json()
                return result.get("text", "").strip()
        finally:
            fh.close()


def _is_mentioned(event) -> bool:
    if event.is_reply:
        return True
    if _my_username and ("@" + _my_username.lower()) in event.raw_text.lower():
        return True
    if event.message.entities:
        from telethon.tl.types import MessageEntityMention, MessageEntityMentionName

        for ent in event.message.entities:
            if isinstance(ent, MessageEntityMentionName) and ent.user_id == _my_id:
                return True
            if isinstance(ent, MessageEntityMention):
                mentioned = event.raw_text[ent.offset : ent.offset + ent.length].lstrip("@").lower()
                if _my_username and mentioned == _my_username.lower():
                    return True
    return False


def _is_service_message(event) -> bool:
    """Return True for Telegram service events such as topic creation."""
    message = getattr(event, "message", None)
    return bool(getattr(message, "action", None))


def _extract_topic_id_from_message(message, *, is_private: bool) -> int | None:
    """Extract forum topic ID from a Telethon message object."""
    if is_private:
        return None
    if message is None:
        return None

    reply_to = getattr(message, "reply_to", None)
    if not reply_to:
        # General topic in forum groups may have no reply_to
        # Check if chat itself is a forum
        return None

    # reply_to_top_id = topic root (when replying to a message within topic)
    top_id = getattr(reply_to, "reply_to_top_id", None)
    if top_id:
        return top_id

    # forum_topic flag = direct message in a topic (not a reply)
    if getattr(reply_to, "forum_topic", False):
        return reply_to.reply_to_msg_id

    # Fallback: reply_to_msg_id might be the topic ID in forum groups
    msg_id = getattr(reply_to, "reply_to_msg_id", None)
    if msg_id:
        return msg_id

    return None


def _extract_topic_id(event) -> int | None:
    """Extract forum topic ID from a Telethon message event.

    In forum supergroups, messages belong to topics. The topic ID
    is used to isolate conversation contexts per topic.

    Private chats can expose ``forum_topic`` reply headers when Telegram
    creates per-chat UI topics. KAOS should treat those as ordinary DMs:
    replying to the topic root produces noisy "topic was created" quotes and
    fragments the private conversation context.

    Telethon bot mode: reply_to.reply_to_msg_id = topic root message ID.
    General topic: reply_to_msg_id = 1 (or absent).
    """
    return _extract_topic_id_from_message(
        getattr(event, "message", None),
        is_private=bool(getattr(event, "is_private", False)),
    )


async def _approval_callback_topic_id(event, pending: dict | None = None) -> int | None:
    """Resolve the topic for an approval callback.

    Bot API callback updates may not expose the original topic metadata in a
    Telethon event, so prefer the durable approval thread id.
    """
    if pending:
        _, topic_id = _chat_topic_from_thread_id(str(pending.get("thread_id", "")))
        if topic_id:
            return topic_id

    message = getattr(event, "message", None)
    if message is None:
        get_message = getattr(event, "get_message", None)
        if callable(get_message):
            try:
                message = await get_message()
            except Exception as e:
                log.debug("Could not fetch callback message for topic routing: %s", e)
    return _extract_topic_id_from_message(
        message,
        is_private=bool(getattr(event, "is_private", False)),
    )


async def _approval_callback_allowed(
    event,
    *,
    sender_id: int,
    pending: dict | None = None,
) -> bool:
    """Return whether a Telegram user may resolve this approval callback."""
    if settings.is_telegram_user_allowed(sender_id):
        return True
    if bool(getattr(event, "is_private", False)):
        return False
    if not pending:
        return False

    pending_chat_id, pending_topic_id = _chat_topic_from_thread_id(str(pending.get("thread_id", "")))
    if pending_chat_id is None or pending_topic_id is None:
        return False

    event_chat_id = getattr(event, "chat_id", None)
    if not _same_telegram_chat(event_chat_id, pending_chat_id):
        return False

    route = _resolve_topic_route(pending_chat_id, pending_topic_id)
    return route.mode == "owner" and _agent_owns_topic(route) and _owner_topic_accepts_sender(sender_id)


def _strip_mention(text: str) -> str:
    if not _my_username:
        return text
    import re

    cleaned = re.sub(r"@" + re.escape(_my_username), "", text, flags=re.IGNORECASE).strip()
    return cleaned if cleaned else text


def _handle_runtime_info_query(text: str) -> str | None:
    """Answer simple runtime/model identity questions deterministically."""
    normalized = " ".join(text.strip().lower().replace("ё", "е").split())
    if not normalized:
        return None

    is_model_question = normalized.startswith("/model") or (
        len(normalized) <= 180
        and any(
            phrase in normalized
            for phrase in (
                "что у тебя за модель",
                "какая у тебя модель",
                "что за модель",
                "на какой модели",
                "какой llm",
                "какой backend",
                "какой бэкенд",
                "какой провайдер",
            )
        )
    )
    if not is_model_question:
        return None

    orchestrator_chain = settings.kaos_orchestrator_provider_chain.strip() or settings.kaos_standard_provider_chain
    return (
        "Сейчас верхний оркестратор KAOS подключён через "
        f"`{orchestrator_chain}`. Для `codex-cli` используется Codex/ChatGPT OAuth "
        f"и модель `{settings.kaos_codex_model}`.\n\n"
        "Важно: это модель оркестратора. Специализированные подагенты пока могут "
        "использовать свои standard/lite цепочки: "
        f"`standard={settings.kaos_standard_provider_chain}`, "
        f"`lite={settings.kaos_lite_provider_chain}`."
    )


async def _fetch_root_user_message(event) -> tuple[str, str]:
    """Walk the reply chain up to find the originating user message.

    Returns ``(text, sender_name)``. Both may be empty strings if the root
    cannot be resolved (e.g. the reply chain is broken, or the root is
    a bot message). The router already guarantees for Tier 3 that the
    immediate parent is a whitelisted user; this helper just fetches its
    contents for inclusion as context.
    """
    try:
        replied = await event.get_reply_message()
    except Exception:
        return "", ""
    if replied is None:
        return "", ""
    text = (replied.raw_text or replied.message or "").strip()
    sender_name = ""
    try:
        sender = await replied.get_sender()
        if sender is not None:
            sender_name = getattr(sender, "first_name", "") or getattr(sender, "username", "") or ""
    except Exception:
        pass
    return text, sender_name


async def _typing_loop(chat_id: int, stop_event: asyncio.Event) -> None:
    """Keep typing indicator active until stop_event is set."""
    try:
        while not stop_event.is_set():
            try:
                async with _client.action(chat_id, "typing"):
                    await asyncio.wait_for(stop_event.wait(), timeout=5.0)
                    return
            except TimeoutError:
                continue  # re-send typing every 5s
    except Exception:
        pass  # typing indicator is best-effort


async def _send_bot_api_message(
    chat_id: int,
    text: str,
    topic_id: int,
    reply_markup: dict | None = None,
) -> int | None:
    """Send message via Bot API with message_thread_id and return first msg id."""
    url = f"https://api.telegram.org/bot{settings.tg_bot_token}/sendMessage"

    chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)] if len(text) > 4000 else [text]
    first_msg_id: int | None = None

    async with aiohttp.ClientSession() as session:
        for chunk in chunks:
            body = {
                "chat_id": chat_id,
                "text": chunk,
                "message_thread_id": topic_id,
                "parse_mode": "Markdown",
            }
            if reply_markup and first_msg_id is None:
                body["reply_markup"] = reply_markup
            try:
                msg_id = await _post_bot_api_message(session, url, body)
                if first_msg_id is None:
                    first_msg_id = msg_id
            except Exception as e:
                log.error("Bot API send error: %s", e)
            if len(chunks) > 1:
                await asyncio.sleep(0.5)

    return first_msg_id


async def _post_bot_api_message(
    session: aiohttp.ClientSession,
    url: str,
    body: dict,
) -> int | None:
    """POST one Bot API message with Markdown and topic fallbacks."""
    timeout = aiohttp.ClientTimeout(total=15)

    async with session.post(url, json=body, timeout=timeout) as resp:
        if resp.status == 200:
            data = await resp.json()
            return int(data.get("result", {}).get("message_id") or 0) or None
        err = await resp.text()

    # Markdown parse error -> retry as plain text.
    plain_body = dict(body)
    plain_body.pop("parse_mode", None)
    async with session.post(url, json=plain_body, timeout=timeout) as retry:
        if retry.status == 200:
            data = await retry.json()
            return int(data.get("result", {}).get("message_id") or 0) or None
        retry_err = await retry.text()
        if retry.status != 400 or "message_thread" not in retry_err:
            log.error("Bot API send failed: %s %s", retry.status, retry_err[:200])
            return None

    # Last resort: send to chat without topic so alerts are not lost.
    no_topic_body = dict(plain_body)
    no_topic_body.pop("message_thread_id", None)
    async with session.post(url, json=no_topic_body, timeout=timeout) as fallback:
        if fallback.status == 200:
            data = await fallback.json()
            log.warning("Bot API sent without topic after message_thread_id failure")
            return int(data.get("result", {}).get("message_id") or 0) or None
        fallback_err = await fallback.text()
        log.error("Bot API send failed after topic fallback: %s %s", fallback.status, fallback_err[:200])
        return None


async def _clear_context(chat_id: int, topic_id: int | None = None) -> str:
    """Clear conversation history for a chat/topic."""
    thread_id = f"{chat_id}:{topic_id}" if topic_id else str(chat_id)
    return await _agent.clear_context(thread_id)


# --- Live progress reporter (roadmap 4.1) ---

_PROGRESS_THROTTLE_SECONDS = 1.6  # keep well under Telegram's edit rate limit
_PROGRESS_TOOL_LABELS: tuple[tuple[str, str], ...] = (
    ("brave", "🔍 ищу в вебе"),
    ("exa", "🔍 ищу в вебе"),
    ("search", "🔍 ищу"),
    ("fetch", "📄 читаю источник"),
    ("content", "📄 читаю источник"),
    ("extract", "📄 разбираю материал"),
    ("transcript", "📄 читаю расшифровку"),
    ("channel", "📡 смотрю каналы"),
    ("digest", "📡 собираю дайджест"),
    ("finance", "💹 считаю финансы"),
    ("expense", "💹 записываю расход"),
    ("memory", "🧠 роюсь в памяти"),
    ("deploy", "🚀 деплою"),
)


def _humanize_tool(name: str) -> str:
    low = name.lower()
    for marker, label in _PROGRESS_TOOL_LABELS:
        if marker in low:
            return label
    return f"🔧 {name}"


def _progress_label(event: str, payload: dict) -> str | None:
    if event == "tool_call":
        return f"{_humanize_tool(str(payload.get('name', '')))}…"
    if event == "tool_approval_required":
        return "⏸️ жду подтверждения…"
    return None


class _ProgressReporter:
    """Edits a single throwaway 'draft' message with live tool progress.

    Lazily materialized: the draft is only sent once there's something to
    report, so fast (no-tool) replies don't flash a throwaway message. It is
    deleted in finish() so the caller's normal send path (validation, approval
    buttons, TTS, chunking) is untouched. All Telegram calls are best-effort —
    progress must never break a real reply.
    """

    def __init__(self, chat_id: int, topic_id: int | None):
        self._chat_id = chat_id
        self._topic_id = topic_id
        self._draft = None
        self._shown = ""
        self._pending: str | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> "_ProgressReporter":
        self._task = asyncio.create_task(self._run())
        return self

    def on_event(self, event: str, payload: dict) -> None:
        # Sync callback invoked from the engine on each tool event.
        label = _progress_label(event, payload)
        if label:
            self._pending = label

    async def _run(self) -> None:
        while not self._stop.is_set():
            pending = self._pending
            if pending and pending != self._shown:
                self._shown = pending
                await self._render(pending)
                # Throttle edits to stay under Telegram's rate limit.
                wait = _PROGRESS_THROTTLE_SECONDS
            else:
                # Nothing to show yet — poll often so the first event surfaces
                # quickly instead of after a full throttle window.
                wait = 0.1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except TimeoutError:
                pass

    async def _render(self, text: str) -> None:
        try:
            if self._draft is None:
                kwargs = {"reply_to": self._topic_id} if self._topic_id else {}
                self._draft = await _client.send_message(self._chat_id, text, **kwargs)
            else:
                await _client.edit_message(self._chat_id, self._draft, text)
        except Exception as e:
            log.debug("Progress render failed (non-fatal): %s", e)

    async def finish(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._draft is not None:
            try:
                await _client.delete_messages(self._chat_id, [self._draft])
            except Exception as e:
                log.debug("Progress draft delete failed (non-fatal): %s", e)


async def _ask_agent(
    message: str,
    chat_id: int,
    user_id: int,
    topic_id: int | None = None,
    source_kind: str = "user",
    persist_user_turn: bool = True,
    extra_system_context: str = "",
    force_tier: str | None = None,
) -> str | None:
    """Send message to KronosAgent and return response text.

    Shows typing indicator while processing. Returns error message
    instead of None on failure (so user always gets feedback).

    When topic_id is provided (forum group), each topic gets its own
    conversation context via separate thread_id.

    source_kind / persist_user_turn / extra_system_context are forwarded
    to KronosAgent.ainvoke — see its docstring. Group transport metadata
    (sender name, "you are in a group chat", peer answer being reacted to)
    must be passed via extra_system_context, NEVER inlined into `message`.
    This is the contract that stops peer text from polluting session
    history and causing verbatim-parrot replies.
    """
    # Topic-aware thread isolation
    thread_id = f"{chat_id}:{topic_id}" if topic_id else str(chat_id)

    # Start typing indicator + live progress reporter
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(chat_id, stop_typing))
    reporter = _ProgressReporter(chat_id, topic_id).start()

    start_ms = int(time.monotonic() * 1000)
    reply = None

    try:
        async with _thread_lock(thread_id), _agent_semaphore:
            reply = await _agent.ainvoke(
                message=message,
                thread_id=thread_id,
                user_id=str(user_id),
                session_id=str(chat_id),
                source_kind=source_kind,
                persist_user_turn=persist_user_turn,
                extra_system_context=extra_system_context,
                on_tool_event=reporter.on_event,
                force_tier=force_tier,
            )
    except Exception as e:
        log.error("Agent error: %s", e)
        reply = "Произошла ошибка при обработке запроса. Попробуй ещё раз."
    finally:
        stop_typing.set()
        typing_task.cancel()
        await reporter.finish()

    if not reply:
        reply = "Не удалось получить ответ от агента. Попробуй переформулировать запрос."

    # Audit log
    duration_ms = int(time.monotonic() * 1000) - start_ms
    from kronos.router import classify_tier

    tier = classify_tier(message).value

    log_request(
        user_id=str(user_id),
        session_id=str(chat_id),
        tier=tier,
        input_text=message,
        output_text=reply,
        duration_ms=duration_ms,
        blocked="заблокирован" in reply,
    )

    return reply


async def _send_to_chat(
    chat_id: int,
    text: str,
    parse_mode: str | None = None,
    topic_id: int | None = None,
) -> None:
    """Send message to Telegram chat with rate limiting and chunking."""
    await _rate_limit_wait(chat_id)
    await _human_typing_delay(chat_id, text)

    kwargs = {}
    if parse_mode:
        kwargs["parse_mode"] = parse_mode
    if topic_id:
        kwargs["reply_to"] = topic_id

    if len(text) > 4000:
        chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]
        for chunk in chunks:
            await _client.send_message(chat_id, chunk, **kwargs)
            await asyncio.sleep(0.5)
    else:
        await _client.send_message(chat_id, text, **kwargs)


# --- Webhook server (for cron scripts, same API as Kronos I) ---


def _webhook_unauthorized(request: web.Request) -> web.Response | None:
    """Fail-closed webhook auth. Returns a 401 response when the request is not
    authorized, else None.

    An empty ``webhook_secret`` DISABLES the endpoint (always 401) instead of
    letting everyone in: the historical ``secret != settings.webhook_secret``
    check silently turned auth off when the secret was "" (``"" == ""``), and
    the server binds a network port — so a missing secret exposed /webhook and
    the chat-dumping /history to anyone who could reach it. Comparison is
    constant-time to avoid leaking the secret via timing.
    """
    expected = settings.webhook_secret
    provided = request.headers.get("X-Webhook-Secret", "")
    if not expected or not secrets.compare_digest(provided, expected):
        return web.json_response({"error": "unauthorized"}, status=401)
    return None


async def _handle_webhook(request: web.Request) -> web.Response:
    if (unauthorized := _webhook_unauthorized(request)) is not None:
        return unauthorized

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    text = body.get("text") or body.get("message") or body.get("content", "")
    chat_id = int(body.get("chat_id", DEFAULT_NOTIFY_CHAT))
    parse_mode = body.get("parse_mode")
    topic_id = body.get("topic_id")
    if topic_id:
        topic_id = int(topic_id)

    if not text:
        return web.json_response({"error": "no text"}, status=400)

    log.info("[Webhook] → chat %d: %s", chat_id, text[:100])
    try:
        await _send_to_chat(chat_id, text, parse_mode=parse_mode, topic_id=topic_id)
        return web.json_response({"ok": True})
    except Exception as e:
        log.error("[Webhook] Send failed: %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def _handle_history(request: web.Request) -> web.Response:
    if (unauthorized := _webhook_unauthorized(request)) is not None:
        return unauthorized

    chat_param = request.query.get("chat", "")
    if not chat_param:
        return web.json_response({"error": "missing 'chat' parameter"}, status=400)

    limit = min(int(request.query.get("limit", "200")), 500)
    offset_id = int(request.query.get("offset_id", "0"))

    try:
        entity = await _client.get_entity(chat_param)
    except Exception:
        return web.json_response({"error": f"chat not found: {chat_param}"}, status=404)

    messages = []
    async for msg in _client.iter_messages(entity, limit=limit, offset_id=offset_id):
        if not msg.text:
            continue
        messages.append(
            {
                "id": msg.id,
                "date": msg.date.isoformat(),
                "text": msg.text,
                "is_outgoing": msg.out,
            }
        )

    return web.json_response(
        {
            "messages": messages,
            "chat": {
                "id": entity.id,
                "username": getattr(entity, "username", None),
                "first_name": getattr(entity, "first_name", None),
            },
            "total": len(messages),
            "has_more": len(messages) == limit,
            "oldest_id": messages[-1]["id"] if messages else 0,
        }
    )


async def _handle_health(request: web.Request) -> web.Response:
    """Live readiness: reflects the Telegram client and provider state instead
    of a static 'ok' (a health-check every 15 min was checking a fiction)."""
    from kronos.llm import active_provider_cooldowns

    connected = bool(_client and _client.is_connected())
    last_event_age = round(time.time() - _last_event_ts, 1) if _last_event_ts else None
    cooldowns = active_provider_cooldowns()

    # Healthy requires the Telegram client to be connected. Provider cooldowns
    # are reported for visibility but don't fail health on their own (the
    # fallback chain still has other providers).
    healthy = connected
    body = {
        "status": "ok" if healthy else "degraded",
        "agent": settings.agent_name,
        "telegram_connected": connected,
        "last_event_age_seconds": last_event_age,
        "providers_in_cooldown": cooldowns,
    }
    return web.json_response(body, status=200 if healthy else 503)


async def _start_webhook_server() -> None:
    app = web.Application()
    app.router.add_post("/webhook", _handle_webhook)
    app.router.add_get("/history", _handle_history)
    app.router.add_get("/health", _handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    webhook_port = int(os.environ.get("WEBHOOK_PORT", "8788"))
    # Bind localhost by default; external exposure is opt-in via WEBHOOK_HOST
    # (e.g. 0.0.0.0). Together with the fail-closed secret check this keeps the
    # userbot's chat-dumping /history off the public network unless explicitly
    # opened AND a secret is set.
    webhook_host = os.environ.get("WEBHOOK_HOST", "127.0.0.1")
    site = web.TCPSite(runner, webhook_host, webhook_port)
    await site.start()
    if webhook_host not in ("127.0.0.1", "localhost", "::1") and not settings.webhook_secret:
        log.warning(
            "Webhook bound to %s with an empty WEBHOOK_SECRET — every /webhook "
            "and /history request is rejected (fail-closed). Set WEBHOOK_SECRET "
            "to serve external clients.",
            webhook_host,
        )
    log.info("Webhook server listening on %s:%d", webhook_host, webhook_port)


# --- /stats command handler (roadmap 6.2) ---


async def _handle_stats_command(text: str) -> str | None:
    """Handle /stats [today|week]. Returns reply text, or None if not /stats."""
    if not text.startswith("/stats"):
        return None

    from kronos.security.cost_stats import cost_report, swarm_cost_by_agent

    parts = text.split()
    period = "week" if len(parts) > 1 and parts[1].lower().startswith("week") else "today"
    period_ru = "неделя" if period == "week" else "сегодня"

    report = cost_report(period)
    total = report["total"]
    status = get_guardian().get_status()

    lines = [f"📊 Расходы ({period_ru}) — {settings.agent_name}"]
    if total["requests"] == 0:
        lines.append("Запросов пока нет.")
    else:
        for tier, stats in sorted(report["by_tier"].items()):
            lines.append(f"• {tier}: {stats['requests']} зпр · ${stats['cost']:.4f}")
        lines.append(f"• Итого: {total['requests']} зпр · ${total['cost']:.4f}")

    daily = status["daily_cost"]
    limit = status["daily_limit"] or 0
    pct = (daily / limit * 100) if limit else 0
    lines.append(f"\nДневной бюджет: ${daily:.2f} / ${limit:.2f} ({pct:.0f}%)")

    swarm = swarm_cost_by_agent(period)
    if len(swarm) > 1:
        lines.append(f"\nПо агентам ({period_ru}):")
        for agent, cost in sorted(swarm.items(), key=lambda kv: -kv[1]):
            lines.append(f"• {agent}: ${cost:.4f}")

    return "\n".join(lines)


# --- ASO command handler ---


async def _handle_aso_command(text: str) -> str | None:
    """Handle /aso commands. Returns reply text or None if not an ASO command."""
    if not text.startswith("/aso"):
        return None

    parts = text.strip().split(maxsplit=2)
    cmd = parts[1] if len(parts) > 1 else "help"

    from kronos.agents.aso import (
        aso_approve,
        aso_reject,
        aso_resume,
        aso_run,
        aso_skip,
        aso_status,
    )

    if cmd == "run":
        dry_run = "--dry-run" in text
        return await aso_run(dry_run=dry_run)
    elif cmd == "approve":
        return await aso_approve()
    elif cmd == "reject":
        comment = parts[2] if len(parts) > 2 else ""
        return await aso_reject(comment)
    elif cmd == "skip":
        return await aso_skip()
    elif cmd == "resume":
        return await aso_resume()
    elif cmd == "status":
        return await aso_status()
    else:
        return (
            "ASO команды:\n"
            "/aso run [--dry-run] — запустить цикл\n"
            "/aso status — текущий статус\n"
            "/aso approve — одобрить план\n"
            "/aso reject <комментарий> — отклонить\n"
            "/aso skip — пропустить цикл\n"
            "/aso resume — продолжить после ожидания"
        )


async def _handle_observer_command(
    text: str,
    *,
    is_dm: bool,
    actor_id: str,
) -> str | None:
    """Handle /observer controls after Telegram access checks."""
    if not text.strip().casefold().startswith("/observer"):
        return None
    if not is_dm:
        log.info("Ignoring /observer command outside DM")
        return ""

    from kronos.observer.commands import handle_observer_command

    return await handle_observer_command(
        text,
        client=_client,
        is_dm=is_dm,
        actor_id=actor_id,
    )


def _is_osint_command(text: str) -> bool:
    return text.strip().casefold().startswith("/osint")


async def _handle_osint_command(
    text: str,
    *,
    is_dm: bool,
) -> str | None:
    """Handle explicit OSINT commands; never in groups."""
    if not _is_osint_command(text):
        return None
    if not is_dm:
        log.info("Ignoring /osint command outside DM")
        return ""

    from kronos.osint.person import handle_osint_command

    return await asyncio.to_thread(handle_osint_command, text)


# --- Main entry ---


async def run_bridge(agent: KronosAgent) -> None:
    """Start Telethon client + webhook server, listen for messages."""
    global _agent, _client, _my_id, _my_username
    _agent = agent

    session_file = os.environ.get("SESSION_FILE", f"{settings.agent_name}.session")
    _client = TelegramClient(session_file, settings.tg_api_id, settings.tg_api_hash)

    is_bot = bool(settings.tg_bot_token)
    log.info("Starting %s bridge (mode: %s)", settings.agent_name, "bot" if is_bot else "userbot")
    log.info("Allowed users: %s", settings.telegram_access_description)

    @_client.on(events.CallbackQuery(pattern=APPROVAL_CALLBACK_PREFIX.encode("utf-8")))
    async def handle_approval_callback(event):
        parsed = _parse_approval_callback_data(getattr(event, "data", b""))
        if parsed is None:
            await event.answer("Unknown approval action", alert=True)
            return

        action, approval_id = parsed
        sender_id = int(getattr(event, "sender_id", 0) or 0)
        if _agent is None:
            await event.answer("Agent is not ready", alert=True)
            return

        pending = await _agent.get_pending_tool_approval(approval_id)
        if not await _approval_callback_allowed(event, sender_id=sender_id, pending=pending):
            log.info("Ignoring approval callback from unauthorized user %s", sender_id)
            await event.answer("Not allowed", alert=True)
            return

        topic_id = await _approval_callback_topic_id(event, pending)
        approved = action == "approve"
        await event.answer("Approved" if approved else "Rejected")
        # Serialize the resolve against new messages on the same thread.
        approval_thread = str(pending["thread_id"]) if pending else f"approval:{approval_id}"
        try:
            async with _thread_lock(approval_thread), _agent_semaphore:
                reply = await _agent.resolve_tool_approval(
                    approval_id,
                    approved=approved,
                    decided_by=str(sender_id),
                )
        except Exception as e:
            log.error("Approval callback failed: %s", e)
            reply = "Не удалось обработать approval callback. Проверь логи."

        validation = validate_output(reply)
        if not validation.is_clean:
            reply = validation.redacted_text

        next_approval_id = _last_pending_approval_id()
        buttons = _approval_buttons(next_approval_id) if next_approval_id else None
        bot_markup = _approval_bot_reply_markup(next_approval_id) if next_approval_id else None
        chat_id = int(getattr(event, "chat_id", 0) or 0)
        if topic_id and settings.tg_bot_token and chat_id:
            await _send_bot_api_message(
                chat_id,
                reply,
                topic_id,
                reply_markup=bot_markup,
            )
        elif topic_id and chat_id:
            await _client.send_message(
                chat_id,
                reply,
                reply_to=topic_id,
                buttons=buttons,
            )
        else:
            await event.respond(reply, buttons=buttons)

    @_client.on(events.NewMessage(incoming=True))
    async def handle_message(event):
        global _last_event_ts
        _last_event_ts = time.time()
        # Log ALL incoming events for debugging
        log.info(
            "[EVENT] chat=%s private=%s reply_to=%s text=%s",
            event.chat_id,
            event.is_private,
            getattr(event.message, "reply_to", None),
            (event.raw_text or "")[:50],
        )

        if _is_service_message(event):
            log.info("Ignoring Telegram service message in chat=%s", event.chat_id)
            return

        sender = await event.get_sender()
        user_id = sender.id
        text = event.raw_text

        if user_id == _my_id:
            return

        is_dm = event.is_private
        voice = _is_voice_message(event)
        image = _is_image_message(event)
        document = _is_document_message(event)

        if not text and not voice and not image and not document:
            return

        # Swarm ledger ingress: record every observed group message before
        # routing, so other agents (and our post-mortem tools) can see it
        # even if this agent decides to skip. DMs stay out of the swarm
        # ledger — they are 1:1 and already isolated per-agent.
        topic_id_inbound = _extract_topic_id(event) if not is_dm else None
        swarm = get_swarm() if not is_dm else None
        if swarm is not None and text:
            reply_to = getattr(event.message, "reply_to", None)
            reply_to_msg_id = getattr(reply_to, "reply_to_msg_id", None) if reply_to else None
            sender_type = "user"
            agent_name_tag: str | None = None
            if _group_router is not None:
                if _group_router._is_peer(user_id):
                    sender_type = "agent"
                    # Reverse lookup via the router's registry (single source of truth).
                    peer_uname = (getattr(sender, "username", "") or "").lower().lstrip("@")
                    agent_name_tag = _group_router._username_to_agent.get(peer_uname) if peer_uname else None
            swarm.record_inbound_message(
                chat_id=event.chat_id,
                topic_id=topic_id_inbound,
                msg_id=event.message.id,
                reply_to_msg_id=reply_to_msg_id,
                sender_id=user_id,
                sender_type=sender_type,
                agent_name=agent_name_tag,
                text=text,
            )

        topic_route = TopicRoute("default")
        if not is_dm:
            topic_route = _resolve_topic_route(event.chat_id, topic_id_inbound)

        # Group filtering
        decision = None
        if not is_dm:
            if not settings.telegram_group_responses_enabled:
                log.info("[TopicPolicy] %s observing group message only", settings.agent_name)
                return

            if topic_route.mode == "silent":
                log.info(
                    "[TopicPolicy] %s skipping chat=%s topic=%s (%s)",
                    settings.agent_name,
                    event.chat_id,
                    topic_id_inbound,
                    topic_route.label,
                )
                return

            if topic_route.mode == "owner":
                if not _agent_owns_topic(topic_route):
                    log.info(
                        "[TopicPolicy] %s skipping owner topic=%s; owner=%s",
                        settings.agent_name,
                        topic_id_inbound,
                        topic_route.owner_agent,
                    )
                    return
                if not _owner_topic_accepts_sender(user_id):
                    log.info(
                        "[TopicPolicy] %s ignoring peer/non-user in owner topic=%s sender=%s",
                        settings.agent_name,
                        topic_id_inbound,
                        user_id,
                    )
                    if swarm is not None:
                        swarm.incr_metric("topic_owner_peer_ignored")
                    return
                decision = TopicDecision(
                    True,
                    0.0,
                    1,
                    f"topic-owner:{topic_route.label}",
                )
                if swarm is not None:
                    swarm.claim_reply(
                        chat_id=event.chat_id,
                        topic_id=topic_id_inbound,
                        root_msg_id=event.message.id,
                        trigger_msg_id=event.message.id,
                        agent_name=settings.agent_name,
                        tier=decision.tier,
                        eta_ts=time.time(),
                        reason=decision.reason,
                    )
            elif _group_router:
                # Multi-agent group routing (all group types)
                decision = await _group_router.decide(event, _client)
                if not decision.should_respond:
                    # Count "skipped because another agent was addressed" as
                    # a successful addressing-correctness event, and count
                    # "skipped because another peer already replied" as a
                    # duplicate-prevention event.
                    if decision.addressing and decision.addressing.explicit_to_other:
                        swarm.incr_metric("addressing_respected")
                    return

                log.info(
                    "[GroupRouter] %s: tier=%d delay=%.0fs reason=%s",
                    settings.agent_name,
                    decision.tier,
                    decision.delay,
                    decision.reason,
                )

                # Resolve root user message id for claim bookkeeping. For
                # user-triggered messages, root = the user's message itself.
                # For peer reactions (Tier 3) we look up the reply parent.
                reply_to = getattr(event.message, "reply_to", None)
                parent_msg_id = getattr(reply_to, "reply_to_msg_id", None) if reply_to else None
                root_msg_id = parent_msg_id if decision.tier == 3 and parent_msg_id else event.message.id

                eta_ts = time.time() + max(decision.delay, 0.0)
                swarm.claim_reply(
                    chat_id=event.chat_id,
                    topic_id=topic_id_inbound,
                    root_msg_id=root_msg_id,
                    trigger_msg_id=event.message.id,
                    agent_name=settings.agent_name,
                    tier=decision.tier,
                    eta_ts=eta_ts,
                    reason=decision.reason,
                )

                if decision.delay > 0:
                    await asyncio.sleep(decision.delay)

                # Post-delay recheck (Tier 2/3) — another agent may have
                # answered while we were waiting.
                still_ok = await _group_router.should_still_respond(
                    event,
                    _client,
                    tier=decision.tier,
                )
                if not still_ok:
                    swarm.cancel_claim(
                        chat_id=event.chat_id,
                        topic_id=topic_id_inbound,
                        trigger_msg_id=event.message.id,
                        agent_name=settings.agent_name,
                        reason="post-delay: peer replied first",
                    )
                    swarm.incr_metric("duplicate_replies_avoided")
                    return

                # Atomic arbitration across all agents.
                outcome = swarm.can_send_claim(
                    chat_id=event.chat_id,
                    topic_id=topic_id_inbound,
                    root_msg_id=root_msg_id,
                    agent_name=settings.agent_name,
                    tier=decision.tier,
                )
                if not outcome.won:
                    log.info("[Swarm] %s stands down: %s", settings.agent_name, outcome.reason)
                    swarm.cancel_claim(
                        chat_id=event.chat_id,
                        topic_id=topic_id_inbound,
                        trigger_msg_id=event.message.id,
                        agent_name=settings.agent_name,
                        reason=outcome.reason,
                    )
                    swarm.incr_metric("duplicate_replies_avoided")
                    return

            else:
                # Fallback: no router — only mentions/replies
                if not _is_mentioned(event):
                    return

        # DM: check allowed users
        if is_dm and not settings.is_telegram_user_allowed(user_id):
            log.info("Ignoring DM from unauthorized Telegram user %s", user_id)
            return

        image_analysis = ""

        # Voice transcription / image analysis
        if voice:
            if not settings.groq_api_key:
                return
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                    tmp_path = tmp.name
                await event.message.download_media(file=tmp_path)
                clean_text = await _transcribe_voice(tmp_path)
                os.unlink(tmp_path)
            except Exception as e:
                log.error("[Voice] Failed: %s", e)
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                return
            if not clean_text:
                return
        elif image:
            clean_text = _strip_mention(text) if not is_dm else text
            try:
                image_analysis = await _analyze_image_message(event, clean_text)
            except Exception as e:
                log.error("[Vision] Failed: %s", e)
                reply = (
                    "Не удалось обработать изображение. "
                    "Проверь, что включён OpenAI/Codex vision provider и формат изображения поддерживается."
                )
                await _send_to_chat(event.chat_id, reply, topic_id=_extract_topic_id(event))
                return
        elif document:
            clean_text = _strip_mention(text) if not is_dm else text
            document_text, doc_error = await _extract_document_message(event)
            if doc_error:
                await _send_to_chat(
                    event.chat_id, f"📄 {doc_error}", topic_id=_extract_topic_id(event)
                )
                return
        else:
            clean_text = _strip_mention(text) if not is_dm else text

        # Build transient system context for group chats. This context is
        # passed to ainvoke(extra_system_context=...) and is NEVER persisted
        # into session history — it disappears after the current LLM call.
        # This is what prevents peer text from being echoed back verbatim.
        group_extra_context = ""
        if image:
            invoke_message = _compose_image_agent_message(clean_text, image_analysis)
        elif document:
            doc_filename, _ = _document_info(event)
            invoke_message = _compose_document_agent_message(
                clean_text, doc_filename, document_text
            )
        else:
            invoke_message = clean_text
        invoke_source_kind = "user"
        invoke_persist = True

        shared_group_context = ""
        if not is_dm and swarm is not None:
            shared_group_context = _format_shared_group_context(
                swarm,
                chat_id=event.chat_id,
                topic_id=topic_id_inbound,
                current_msg_id=event.message.id,
            )

        if not is_dm and topic_route.mode == "owner" and decision is not None:
            sender_name = sender.first_name or "Unknown"
            group_extra_context = (
                f"[Закрепленный Telegram-топик: {topic_route.label}] "
                f"Сообщение от {sender_name}. Этот топик закреплен за агентом "
                f"{settings.agent_name}; остальные агенты должны молчать. "
                f"Отвечай напрямую, без требования тегать тебя, и учитывай историю топика."
            )
        elif not is_dm and _group_router and decision is not None:
            sender_name = sender.first_name or "Unknown"
            is_peer_sender = _group_router._is_peer(user_id)

            if decision.tier == 3 and is_peer_sender:
                # Peer reaction: reframe the call so the agent responds to
                # the *root user question*, treating the peer's answer as
                # context to compare against — not as a new question.
                root_user_text, root_user_name = await _fetch_root_user_message(event)
                invoke_message = root_user_text or clean_text
                invoke_source_kind = "peer_reaction"
                invoke_persist = False  # ephemeral — don't pollute history
                peer_snippet = clean_text[:1500]
                group_extra_context = (
                    f"[Групповой чат] Пользователь{f' ({root_user_name})' if root_user_name else ''} "
                    f"задал вопрос. Другой агент ({sender_name}) уже ответил:\n"
                    f"---\n{peer_snippet}\n---\n"
                    f"Добавь МЕНЯЮЩУЮ смысл дельту только если у тебя есть "
                    f"критически важный иной угол. Если твой ответ по сути "
                    f"повторяет коллегу — промолчи и ответь одним словом 'PASS'. "
                    f"Никогда не цитируй и не перефразируй ответ коллеги целиком. "
                    f"Говори от своего лица, 2-3 коротких абзаца максимум."
                )
            else:
                # Regular user message in group chat: tell the agent it's in
                # a multi-agent room; keep the raw user text as `message`.
                group_extra_context = (
                    f"[Групповой чат] Сообщение от {sender_name}. "
                    f"Отвечай по существу, от своего лица, коротко (2-4 абзаца). "
                    f"Не комментируй ответы других агентов, если они есть. "
                    f"Не описывай свой процесс мышления или фреймворки."
                )

        if not is_dm and shared_group_context:
            group_extra_context = "\n\n".join(part for part in (group_extra_context, shared_group_context) if part)

        # Extract forum topic_id (for topic-based context isolation)
        topic_id = _extract_topic_id(event)

        chat_type = "group" if not is_dm else "DM"
        topic_label = f" topic={topic_id}" if topic_id else ""
        log.info("[%s%s] %s (%d): %s", chat_type, topic_label, sender.first_name, user_id, clean_text[:100])

        if is_dm and not is_bot:
            await _rate_limit_wait(event.chat_id)
            try:
                await _client.send_read_acknowledge(event.chat_id, event.message)
            except Exception:
                pass  # Bot API sessions can't call ReadHistoryRequest

        if await _maybe_record_observer_capture(
            event,
            clean_text,
            is_dm=is_dm,
            voice=voice,
            image=image,
            user_id=user_id,
        ):
            return

        # /clear command — reset conversation context for this chat/topic
        if clean_text.strip().lower() in ("/clear", "/reset"):
            reply = await _clear_context(event.chat_id, topic_id)
        # /voice command — toggle voice mode
        elif clean_text.strip().lower().startswith("/voice"):
            arg = clean_text.strip().lower().removeprefix("/voice").strip()
            if arg == "on":
                set_voice_mode(event.chat_id, True)
                reply = "Голосовой режим включён. Буду отвечать голосом на короткие сообщения."
            elif arg == "off":
                set_voice_mode(event.chat_id, False)
                reply = "Голосовой режим выключен. Голосом отвечаю только на голосовые."
            else:
                current = get_voice_mode(event.chat_id)
                set_voice_mode(event.chat_id, not current)
                if not current:
                    reply = "Голосовой режим включён. Буду отвечать голосом на короткие сообщения."
                else:
                    reply = "Голосовой режим выключен. Голосом отвечаю только на голосовые."
        elif (runtime_reply := _handle_runtime_info_query(clean_text)) is not None:
            reply = runtime_reply
        elif (
            observer_reply := await _handle_observer_command(
                clean_text,
                is_dm=is_dm,
                actor_id=str(user_id),
            )
        ) is not None:
            if not observer_reply:
                return
            reply = observer_reply
        elif (stats_reply := await _handle_stats_command(clean_text)) is not None:
            reply = stats_reply
        elif _is_osint_command(clean_text) and not is_dm:
            log.info("Ignoring /osint command outside DM")
            return
        # Cost guardian check
        else:
            guardian = get_guardian()
            allowed, budget_msg = guardian.check_budget(session_id=str(event.chat_id))
            if not allowed:
                reply = f"⚠️ {budget_msg}"
            # Intercept /aso commands before agent
            elif (aso_reply := await _handle_aso_command(clean_text)) is not None:
                reply = aso_reply
            elif (
                osint_reply := await _handle_osint_command(
                    clean_text,
                    is_dm=is_dm,
                )
            ) is not None:
                if not osint_reply:
                    return
                reply = osint_reply
            else:
                # Soft cost degradation: once daily spend crosses the degrade
                # threshold, force the lite tier instead of blocking.
                degrade_tier = "lite" if guardian.should_degrade() else None
                # Call agent with new contract: raw user text as message,
                # group metadata as transient extra_system_context only.
                reply = await _ask_agent(
                    invoke_message,
                    event.chat_id,
                    user_id,
                    topic_id=topic_id,
                    source_kind=invoke_source_kind,
                    persist_user_turn=invoke_persist,
                    extra_system_context=group_extra_context,
                    force_tier=degrade_tier,
                )

        # Peer-reaction "PASS" protocol: the agent is instructed to reply
        # with "PASS" when it has nothing meaningfully different to add.
        # Treat that as a no-op — do not send anything to the chat.
        if invoke_source_kind == "peer_reaction" and reply:
            stripped = reply.strip().strip("'\"`.!").upper()
            if stripped == "PASS" or stripped.startswith("PASS"):
                log.info("[%s%s] Peer-reaction PASS from %s", chat_type, topic_label, settings.agent_name)
                if swarm is not None:
                    swarm.cancel_claim(
                        chat_id=event.chat_id,
                        topic_id=topic_id_inbound,
                        trigger_msg_id=event.message.id,
                        agent_name=settings.agent_name,
                        reason="peer-reaction self-pass",
                    )
                return

        # Output validation — redact secrets, log issues
        validation = validate_output(reply)
        if not validation.is_clean:
            reply = validation.redacted_text

        approval_id = _last_pending_approval_id()
        approval_buttons = _approval_buttons(approval_id) if approval_id else None
        approval_bot_markup = _approval_bot_reply_markup(approval_id) if approval_id else None

        await _rate_limit_wait(event.chat_id)

        # Topic messages: reply_to = topic root so message lands in correct topic
        # Regular DM: no reply_to needed
        # Regular group: reply to the user's message
        if topic_id:
            reply_to = topic_id  # sends into the topic thread
        elif not is_dm:
            reply_to = event.message.id  # reply in group
        else:
            reply_to = None

        # TTS: voice mode always, or mirror user's voice message
        voice_sent = False
        vm = get_voice_mode(event.chat_id)
        if not approval_id and should_synthesize(reply, user_sent_voice=voice, voice_mode=vm):
            voice_path = await synthesize(reply)
            if voice_path:
                try:
                    await _client.send_file(
                        event.chat_id,
                        voice_path,
                        voice_note=True,
                        reply_to=reply_to,
                    )
                    voice_sent = True
                except Exception as e:
                    log.error("Voice send failed: %s", e)
                finally:
                    if os.path.exists(voice_path):
                        os.unlink(voice_path)

        sent_msg = None
        sent_msg_id: int | None = None
        if not voice_sent:
            if topic_id and settings.tg_bot_token:
                # Use Bot API with message_thread_id when a bot token is configured.
                sent_msg_id = await _send_bot_api_message(
                    event.chat_id,
                    reply,
                    topic_id,
                    reply_markup=approval_bot_markup,
                )
            elif topic_id:
                sent_msg = await _client.send_message(
                    event.chat_id,
                    reply,
                    reply_to=topic_id,
                    buttons=approval_buttons,
                )
            elif len(reply) > 4000:
                chunks = [reply[i : i + 4000] for i in range(0, len(reply), 4000)]
                for i, chunk in enumerate(chunks):
                    sent = await _client.send_message(
                        event.chat_id,
                        chunk,
                        reply_to=event.message.id if i == 0 and not is_dm else None,
                        buttons=approval_buttons if i == 0 else None,
                    )
                    if i == 0:
                        sent_msg = sent
                    await asyncio.sleep(0.5)
            else:
                reply_to_msg = event.message.id if not is_dm else None
                sent_msg = await _client.send_message(
                    event.chat_id,
                    reply,
                    reply_to=reply_to_msg,
                    buttons=approval_buttons,
                )

        # Swarm ledger: mark claim as sent, record outbound message. DMs
        # and fallback-router paths (no decision) skip this.
        if not is_dm and swarm is not None and decision is not None:
            reply_msg_id = sent_msg_id or (getattr(sent_msg, "id", None) if sent_msg is not None else None)
            swarm.mark_sent(
                chat_id=event.chat_id,
                topic_id=topic_id_inbound,
                trigger_msg_id=event.message.id,
                agent_name=settings.agent_name,
                reply_msg_id=reply_msg_id,
            )
            if reply_msg_id is not None:
                swarm.record_outbound_message(
                    chat_id=event.chat_id,
                    topic_id=topic_id_inbound,
                    msg_id=reply_msg_id,
                    reply_to_msg_id=event.message.id,
                    agent_name=settings.agent_name,
                    text=reply,
                )
            # Metrics: tier breakdown of actual replies (count after-send
            # so failed sends don't inflate the denominator).
            swarm.incr_metric(f"replies_tier{decision.tier}")
            swarm.incr_metric("replies_total")

        reply_mode = "voice" if voice_sent else "text"
        log.info("[%s%s] Replied (%s) to %s: %s", chat_type, topic_label, reply_mode, sender.first_name, reply[:100])

    # --- Reaction handler (RL feedback loop) ---

    from telethon.tl.types import UpdateMessageReactions

    @_client.on(events.Raw(types=UpdateMessageReactions))
    async def handle_reaction(event: UpdateMessageReactions):
        """Handle Telegram reactions (👍/👎) for RL feedback."""
        try:
            chat_id = None
            # Extract chat_id from the peer
            peer = event.peer
            if hasattr(peer, "channel_id"):
                chat_id = peer.channel_id
            elif hasattr(peer, "chat_id"):
                chat_id = peer.chat_id
            elif hasattr(peer, "user_id"):
                chat_id = peer.user_id

            if not chat_id:
                return

            msg_id = event.msg_id

            # Get the reactions list
            reactions = event.reactions
            if not reactions or not reactions.results:
                return

            # Find our agent's outbound message in swarm_messages
            swarm = get_swarm()
            rows = swarm._db.read(
                """
                SELECT agent_name FROM swarm_messages
                WHERE msg_id = ? AND (chat_id = ? OR chat_id = ?)
                  AND sender_type = 'agent'
                LIMIT 1
                """,
                (msg_id, chat_id, -chat_id),
            )

            if not rows:
                # Not our message, skip
                return

            agent_name = rows[0]["agent_name"] or settings.agent_name

            # Process each reaction
            for r in reactions.results:
                emoticon = getattr(r.reaction, "emoticon", None)
                if not emoticon:
                    continue

                swarm.add_feedback(
                    agent_name=agent_name,
                    chat_id=chat_id,
                    msg_id=msg_id,
                    emoji=emoticon,
                )
                log.info(
                    "[Feedback] %s on msg %d in chat %d: %s",
                    agent_name,
                    msg_id,
                    chat_id,
                    emoticon,
                )
        except Exception as e:
            log.warning("Reaction handler error (non-fatal): %s", e)

    if is_bot:
        await _client.start(bot_token=settings.tg_bot_token)
    else:
        await _client.start()
    me = await _client.get_me()
    _my_id = me.id
    _my_username = me.username
    log.info("Logged in as: %s (@%s, %d)", me.first_name, me.username, me.id)

    # Initialize group router for multi-agent chats
    global _group_router
    from kronos.group_router import GroupRouter

    _group_router = GroupRouter(
        agent_name=settings.agent_name,
        my_id=_my_id,
        my_username=_my_username,
        allowed_user_ids=settings.allowed_user_ids,
    )
    log.info("Group router initialized for %s", settings.agent_name)

    # Share client for cron jobs and other modules
    from kronos.telegram_client import set_client

    set_client(_client)

    await _start_webhook_server()

    log.info("Listening for messages and webhooks...")
    await _client.run_until_disconnected()
