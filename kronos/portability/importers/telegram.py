"""Import a Telegram Desktop export (`result.json`).

Chats are private by nature, so this importer is opt-in twice over: nothing is
imported unless the caller names the chats (``chats=[…]``), and message text is
masked as third-party content by the bundle builder.

`personal_information` — present in a full export — becomes facts about the
owner: name, username, bio. That block is the one place a Telegram export states
who the account belongs to.
"""

import json
import logging
from pathlib import Path

from kronos.portability.build import BundleBuilder
from kronos.portability.manifest import BundleError

log = logging.getLogger("kronos.portability.importers.telegram")

NAME = "telegram"
_RESULT_JSON = "result.json"

_MAX_JSON_BYTES = 256 * 1024 * 1024
_DETECT_PROBE_BYTES = 8192
_DEFAULT_MESSAGES_PER_CHAT = 500


def detect(path: Path) -> bool:
    """True for result.json (or a directory holding it) that mentions chats.

    Only the first few KB are probed: a full export can be hundreds of MB and
    detection must stay cheap.
    """
    target = path / _RESULT_JSON if path.is_dir() else path
    if not target.is_file() or target.name != _RESULT_JSON:
        return False
    try:
        with open(target, encoding="utf-8", errors="ignore") as handle:
            probe = handle.read(_DETECT_PROBE_BYTES)
    except OSError:
        return False
    return '"chats"' in probe or '"personal_information"' in probe


def _text_of(message: dict) -> str:
    """Flatten Telegram's text field, which is a string or a list of entities."""
    value = message.get("text")
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
        return "".join(parts).strip()
    return ""


def _load(path: Path) -> dict:
    target = path / _RESULT_JSON if path.is_dir() else path
    if not target.is_file():
        raise BundleError(f"not found: {target}")
    if target.stat().st_size > _MAX_JSON_BYTES:
        raise BundleError(
            f"{_RESULT_JSON} is {target.stat().st_size // 1024 // 1024} MB, above the "
            f"{_MAX_JSON_BYTES // 1024 // 1024} MB import limit — export fewer chats"
        )
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise BundleError(f"{_RESULT_JSON} is not valid JSON: {e}") from e
    if not isinstance(payload, dict):
        raise BundleError(f"{_RESULT_JSON} does not contain an export object")
    return payload


def _owner_facts(builder: BundleBuilder, payload: dict, *, user_id: str) -> None:
    info = payload.get("personal_information")
    if not isinstance(info, dict):
        return
    name = " ".join(str(info.get(key) or "").strip() for key in ("first_name", "last_name")).strip()
    if name:
        builder.add_fact(f"Telegram account owner is {name}", user_id=user_id, source="telegram")
    username = str(info.get("username") or "").strip()
    if username:
        builder.add_fact(f"Telegram username is @{username}", user_id=user_id, source="telegram")
    bio = str(info.get("bio") or "").strip()
    if bio:
        builder.add_fact(f"Telegram bio: {bio}", user_id=user_id, source="telegram")


def to_bundle(
    path: str | Path,
    out_path: str | Path,
    *,
    limit: int | None = None,
    user_id: str = "",
    created_at: str = "",
    chats: list[str] | None = None,
):
    """Convert selected chats of a Telegram export into a bundle.

    ``chats`` matches chat name or id; without it no conversation is imported —
    silently ingesting every private chat someone ever had is not a sane default.
    """
    from kronos.config import settings
    from kronos.portability.importers import ImporterResult

    payload = _load(Path(path))
    builder = BundleBuilder(agent_name=NAME, created_at=created_at)
    owner = user_id or settings.agent_name
    _owner_facts(builder, payload, user_id=owner)

    wanted = {str(item).strip().lower() for item in (chats or []) if str(item).strip()}
    per_chat = limit or _DEFAULT_MESSAGES_PER_CHAT
    chat_list = ((payload.get("chats") or {}).get("list")) or []
    matched = 0

    for chat in chat_list:
        if not isinstance(chat, dict):
            continue
        name = str(chat.get("name") or "").strip()
        chat_id = str(chat.get("id") or "").strip()
        if not wanted or not ({name.lower(), chat_id.lower()} & wanted):
            continue

        matched += 1
        history = []
        for message in chat.get("messages") or []:
            if not isinstance(message, dict) or message.get("type") != "message":
                continue
            text = _text_of(message)
            if not text:
                continue
            sender = str(message.get("from") or "").strip()
            history.append({"type": "human", "content": f"{sender}: {text}" if sender else text})

        if len(history) > per_chat:
            builder.warnings.append(f"chat '{name or chat_id}': kept the last {per_chat} messages")
            history = history[-per_chat:]
        if history:
            builder.add_session(f"telegram:{chat_id or name}", history)

    if wanted and not matched:
        builder.warnings.append(f"no chats matched: {', '.join(sorted(wanted))}")
    if not wanted:
        builder.warnings.append("no chats selected — pass chats=[…] to import conversations")

    if builder.is_empty():
        raise BundleError("Telegram export produced nothing importable (select chats or check the export)")

    bundle, manifest = builder.write(out_path)
    log.info("Telegram import: %d chats, %s", matched, builder.counts())
    return ImporterResult(
        importer=NAME,
        bundle=bundle,
        manifest=manifest,
        counts=builder.counts(),
        warnings=builder.warnings,
    )
