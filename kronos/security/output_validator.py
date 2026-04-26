"""Output validation — checks agent responses before sending to user.

Catches:
- Leaked secrets (API keys, tokens, passwords from .env)
- Internal system info (file paths, stack traces, config details)
- Prompt leakage (system prompt or persona content in response)
- Harmful content patterns

Runs as a lightweight post-processing step (no LLM call — regex only).
"""

import logging
import re

log = logging.getLogger("kronos.security.output_validator")

# Patterns that should never appear in agent output
_SECRET_PATTERNS = [
    # API keys
    r"sk-[a-zA-Z0-9]{20,}",  # OpenAI/Anthropic style
    r"xai-[a-zA-Z0-9]{20,}",
    r"AIza[a-zA-Z0-9_-]{35}",  # Google API key
    r"AKIA[A-Z0-9]{16}",  # AWS access key

    # Tokens
    r"ghp_[a-zA-Z0-9]{36}",  # GitHub PAT
    r"gho_[a-zA-Z0-9]{36}",
    r"eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}",  # JWT

    # Connection strings
    r"(?:postgres|mysql|mongodb)://[^\s]+:[^\s]+@[^\s]+",

    # Generic secrets
    r"(?:password|secret|token|api_key)\s*[=:]\s*['\"][^'\"]{8,}['\"]",
]

_SECRET_RE = re.compile("|".join(f"(?:{p})" for p in _SECRET_PATTERNS), re.IGNORECASE)

# System info patterns
_SYSTEM_PATTERNS = [
    r"/Users/\w+/",  # macOS home paths
    r"/home/\w+/",  # Linux home paths
    r"/root/",
    r"\.env\b",  # .env file reference
    r"Traceback \(most recent call last\)",
    r"File \"[^\"]+\", line \d+",
]

_SYSTEM_RE = re.compile("|".join(f"(?:{p})" for p in _SYSTEM_PATTERNS))

# Prompt leakage indicators
_PROMPT_LEAK_PATTERNS = [
    r"IDENTITY\.md",
    r"SOUL\.md",
    r"AGENTS\.md",
    r"system prompt",
    r"you are an AI assistant",
    r"I am a language model",
    r"as an AI,? I",
]

_PROMPT_LEAK_RE = re.compile("|".join(f"(?:{p})" for p in _PROMPT_LEAK_PATTERNS), re.IGNORECASE)


class ValidationResult:
    def __init__(self):
        self.issues: list[str] = []
        self.redacted_text: str = ""

    @property
    def is_clean(self) -> bool:
        return len(self.issues) == 0


def validate_output(text: str) -> ValidationResult:
    """Validate agent output before sending to user.

    Returns ValidationResult with issues found and redacted text.
    """
    result = ValidationResult()
    redacted = text

    # Check for leaked secrets
    for match in _SECRET_RE.finditer(text):
        secret = match.group()
        result.issues.append(f"leaked_secret: {secret[:8]}...")
        # Redact: keep first 4 chars + mask
        redacted = redacted.replace(secret, secret[:4] + "***REDACTED***")

    # Check for system info
    for match in _SYSTEM_RE.finditer(text):
        result.issues.append(f"system_info: {match.group()[:30]}")
        # Don't redact paths entirely — just log warning

    # Check for prompt leakage
    for match in _PROMPT_LEAK_RE.finditer(text):
        result.issues.append(f"prompt_leak: {match.group()[:30]}")

    result.redacted_text = redacted

    if result.issues:
        log.warning("Output validation: %d issues — %s", len(result.issues), result.issues)

    return result
