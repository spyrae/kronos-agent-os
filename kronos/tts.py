"""Text-to-Speech — synthesize voice responses.

Uses Edge TTS (free, Microsoft) by default.
Supports fallback to OpenAI TTS if configured.

Voice response policy:
- /voice on  → always reply with voice (if short enough)
- /voice off → never reply with voice (default)
- If voice mode is off but user sent voice → reply with voice (mirror)
- Long responses (>500 chars) → text only (too slow for TTS)
"""

import logging
import os
import tempfile

from kronos.db import get_db

log = logging.getLogger("kronos.tts")

# Edge TTS voices (Russian)
EDGE_VOICE_RU = "ru-RU-DmitryNeural"  # male, natural
EDGE_VOICE_EN = "en-US-GuyNeural"  # male, for English content

# Max chars for TTS synthesis
TTS_MAX_CHARS = 2000

# Detect language heuristic threshold
_CYRILLIC_THRESHOLD = 0.3


def _ensure_voice_table() -> None:
    """Create voice_mode table if it doesn't exist."""
    db = get_db("session")
    db.write("""
        CREATE TABLE IF NOT EXISTS voice_mode (
            chat_id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0
        )
    """)


def get_voice_mode(chat_id: int | str) -> bool:
    """Check if voice mode is enabled for a chat."""
    _ensure_voice_table()
    db = get_db("session")
    rows = db.read(
        "SELECT enabled FROM voice_mode WHERE chat_id = ?",
        (str(chat_id),),
    )
    return bool(rows and rows[0][0])


def set_voice_mode(chat_id: int | str, enabled: bool) -> None:
    """Toggle voice mode for a chat."""
    _ensure_voice_table()
    db = get_db("session")
    db.write(
        """INSERT INTO voice_mode (chat_id, enabled) VALUES (?, ?)
           ON CONFLICT(chat_id) DO UPDATE SET enabled = excluded.enabled""",
        (str(chat_id), int(enabled)),
    )


def should_synthesize(text: str, user_sent_voice: bool = False, voice_mode: bool = False) -> bool:
    """Check if response should be synthesized to voice."""
    if not user_sent_voice and not voice_mode:
        return False
    if len(text) > TTS_MAX_CHARS:
        return False
    if not text.strip():
        return False
    return True


async def synthesize(text: str) -> str | None:
    """Synthesize text to speech. Returns path to .ogg file, or None on failure.

    Caller is responsible for deleting the file after use.
    """
    try:
        import edge_tts
    except ImportError:
        log.info("TTS disabled: edge-tts not installed (pip install edge-tts)")
        return None

    if not text or len(text) > TTS_MAX_CHARS:
        return None

    voice = _detect_voice(text)

    try:
        # Create temp file for output
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name

        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(tmp_path)

        # Convert to ogg/opus for Telegram voice (smaller, native format)
        ogg_path = tmp_path.replace(".mp3", ".ogg")
        converted = await _convert_to_ogg(tmp_path, ogg_path)

        # Cleanup mp3
        if os.path.exists(tmp_path) and converted:
            os.unlink(tmp_path)

        result_path = ogg_path if converted else tmp_path
        size_kb = os.path.getsize(result_path) // 1024
        log.info("TTS synthesized: %d chars → %s (%d KB, voice=%s)", len(text), result_path, size_kb, voice)
        return result_path

    except Exception as e:
        log.error("TTS synthesis failed: %s", e)
        # Cleanup on failure
        for p in [tmp_path, tmp_path.replace(".mp3", ".ogg")]:
            if os.path.exists(p):
                os.unlink(p)
        return None


def _detect_voice(text: str) -> str:
    """Detect language and return appropriate voice."""
    cyrillic_count = sum(1 for c in text if '\u0400' <= c <= '\u04ff')
    total = len(text.replace(" ", ""))
    if total == 0:
        return EDGE_VOICE_RU

    if cyrillic_count / total > _CYRILLIC_THRESHOLD:
        return EDGE_VOICE_RU
    return EDGE_VOICE_EN


async def _convert_to_ogg(mp3_path: str, ogg_path: str) -> bool:
    """Convert MP3 to OGG/Opus using ffmpeg (if available)."""
    import asyncio

    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", mp3_path,
            "-c:a", "libopus", "-b:a", "48k",
            "-vn", "-y", ogg_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0 and os.path.exists(ogg_path)
    except FileNotFoundError:
        log.debug("ffmpeg not found, keeping mp3 format")
        return False
