"""Media ingest helpers for the bridge: voice, image, and document.

Type detection, download/extract, and agent-message composition for Telegram
media — extracted verbatim from ``bridge.py``. None of these touch the live
client/agent module state, so they move cleanly and are re-exported from
``kronos.bridge``.
"""

import logging
import os
import tempfile
import time

import aiohttp
from telethon.tl.types import DocumentAttributeAudio

from kronos.config import settings
from kronos.vision import analyze_image_bytes, is_supported_image_mime, is_vision_configured

# Logger name kept as "kronos.bridge" so extracted log lines are unchanged.
log = logging.getLogger("kronos.bridge")

# Groq Whisper STT
GROQ_WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_WHISPER_MODEL = "whisper-large-v3-turbo"

_DOC_EXTENSIONS = (".pdf", ".docx", ".txt", ".md", ".markdown")


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
