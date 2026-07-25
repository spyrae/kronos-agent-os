"""Redaction for exported bundles — strip credentials, keep the content usable.

Two levels, because the two kinds of payload need different treatment:

* ``redact_text`` — removes credentials only. Used for the owner's own material
  (persona, notes, extracted facts, graph): masking the owner's own email out of
  "his address is …" would destroy exactly the value the bundle exists to carry.
* ``redact_structure`` — credentials **and** PII, recursively. Used for session
  history and tool output, which contain third-party data the owner never
  authored.

Neither truncates. ``audit.redact_tool_payload`` clips to 500 chars because logs
are for humans skimming; a bundle must round-trip.
"""

from typing import Any

from kronos.audit import SECRET_FIELD_NAMES, redact_secrets
from kronos.security.pii import mask_pii

_SECRET_KEY_SUFFIXES = ("_token", "_secret", "_password", "_api_key", "_key")
_REDACTED = "***REDACTED***"


def redact_text(text: str) -> str:
    """Strip credentials from owner-authored text, preserving everything else."""
    return redact_secrets(text)


def redact_private_text(text: str) -> str:
    """Strip credentials and mask PII — for third-party content."""
    return mask_pii(redact_secrets(text))


def _is_secret_key(key: str) -> bool:
    name = key.lower().replace("-", "_")
    return name in SECRET_FIELD_NAMES or name.endswith(_SECRET_KEY_SUFFIXES)


def redact_structure(value: Any, *, mask_personal: bool = True, key: str = "") -> Any:
    """Recursively redact a JSON-like structure without truncating anything.

    Values under secret-looking keys are replaced wholesale; strings elsewhere
    are cleaned by ``redact_private_text`` (or ``redact_text`` when
    ``mask_personal`` is False).
    """
    if key and _is_secret_key(key):
        return _REDACTED
    if isinstance(value, dict):
        return {str(k): redact_structure(v, mask_personal=mask_personal, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_structure(item, mask_personal=mask_personal) for item in value]
    if isinstance(value, tuple):
        return [redact_structure(item, mask_personal=mask_personal) for item in value]
    if isinstance(value, str):
        return redact_private_text(value) if mask_personal else redact_text(value)
    return value
