"""Prompt injection shield — ported from Kronos I worker.cjs.

23 regex patterns detecting: instruction override, role manipulation,
system prompt extraction, credential extraction, encoding tricks.
"""

import logging
import re
import time
from dataclasses import dataclass, field

log = logging.getLogger("kronos.security")

# All patterns block by default
INJECTION_PATTERNS: list[re.Pattern] = [
    # Direct instruction override
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I),
    re.compile(r"ignore\s+(all\s+)?above", re.I),
    re.compile(r"disregard\s+(all\s+)?previous", re.I),
    re.compile(r"forget\s+(all\s+)?(your|previous)\s+(instructions|rules|constraints)", re.I),
    re.compile(r"override\s+(system|safety|security)", re.I),
    # Role manipulation
    re.compile(r"you\s+are\s+now\s+(DAN|a\s+new|an?\s+unrestricted)", re.I),
    re.compile(r"pretend\s+(you\s+are|to\s+be)\s+(a\s+different|an?\s+evil|an?\s+unrestricted)", re.I),
    re.compile(r"act\s+as\s+if\s+(you\s+have\s+no|there\s+are\s+no)\s+(rules|restrictions|limits)", re.I),
    re.compile(r"enter\s+(DAN|jailbreak|developer)\s+mode", re.I),
    re.compile(r"switch\s+to\s+(unrestricted|unsafe|uncensored)", re.I),
    # System prompt extraction
    re.compile(r"show\s+(me\s+)?(your\s+)?(system\s+prompt|instructions|rules)", re.I),
    re.compile(r"what\s+are\s+your\s+(system|hidden|secret)\s+(instructions|rules|prompt)", re.I),
    re.compile(r"repeat\s+(your\s+)?(system\s+prompt|initial\s+instructions)", re.I),
    re.compile(r"print\s+(your\s+)?system\s+prompt", re.I),
    # Secret/credential extraction
    re.compile(r"show\s+(me\s+)?(your\s+)?(api\s+key|token|password|secret|\.env)", re.I),
    re.compile(r"reveal\s+(your\s+)?(credentials|secrets|keys)", re.I),
    re.compile(r"what\s+is\s+(your|the)\s+(api\s+key|token|password)", re.I),
    re.compile(r"cat\s+\.env", re.I),
    re.compile(r"echo\s+\$[A-Z_]+KEY", re.I),
    # Encoding tricks
    re.compile(r"base64\s+decode", re.I),
    re.compile(r"eval\s*\(", re.I),
    re.compile(r"exec\s*\(", re.I),
    re.compile(r"__import__", re.I),
    # Russian injection patterns
    re.compile(r"игнорируй\s+(все\s+)?(предыдущие|прошлые)\s+(инструкции|правила)", re.I),
    re.compile(r"забудь\s+(все\s+)?(свои\s+)?(инструкции|правила|ограничения)", re.I),
    re.compile(r"покажи\s+(свой\s+)?(системный\s+промпт|инструкции)", re.I),
    re.compile(r"покажи\s+(api|токен|пароль|ключ|\.env)", re.I),
    re.compile(r"ты\s+теперь\s+(другой|новый|свободный|без\s+ограничений)", re.I),
]

BLOCK_MESSAGE = "Запрос заблокирован системой безопасности."


@dataclass
class RateLimiter:
    """Per-source rate limiter: max_requests per window_seconds."""

    max_requests: int = 10
    window_seconds: float = 60.0
    _buckets: dict[str, list[float]] = field(default_factory=dict)

    def check(self, source: str) -> bool:
        now = time.monotonic()
        bucket = self._buckets.setdefault(source, [])
        cutoff = now - self.window_seconds
        self._buckets[source] = [t for t in bucket if t > cutoff]
        if len(self._buckets[source]) >= self.max_requests:
            return False
        self._buckets[source].append(now)
        return True


rate_limiter = RateLimiter()


def check_injection(message: str) -> list[str]:
    """Check message against injection patterns. Returns list of matched pattern sources."""
    matched = []
    for pattern in INJECTION_PATTERNS:
        if pattern.search(message):
            matched.append(pattern.pattern)
    return matched


def validate_input(message: str, source: str = "default") -> str | None:
    """Validate input message. Returns None if safe, or rejection message if blocked."""
    # Injection check
    matches = check_injection(message)
    if matches:
        log.warning("[Shield] Injection blocked from %s: %s", source, matches[:3])
        return BLOCK_MESSAGE

    # Rate limit check
    if not rate_limiter.check(source):
        log.warning("[Shield] Rate limited: %s", source)
        return "Слишком много запросов. Подожди минуту."

    return None
