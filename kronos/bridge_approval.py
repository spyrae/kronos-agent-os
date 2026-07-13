"""Approval callback encoding + inline-keyboard markup for the bridge.

Pure helpers extracted from ``bridge.py``: the compact callback payload that
Telegram inline buttons carry, plus the Telethon and Bot API keyboard markup.
Re-exported from ``kronos.bridge`` for backward-compatible imports.
"""

from telethon import Button

APPROVAL_CALLBACK_PREFIX = "kaos:approval:"


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
