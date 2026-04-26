"""Tool loop detection — catches agent stuck in repeated tool calls.

Detectors:
- generic_repeat: same tool + args called multiple times
- ping_pong: alternating between two tools without progress
- poll_no_progress: same tool, same result (polling)

Levels:
- WARNING (threshold): inject nudge message
- CRITICAL (2x threshold): force strategy switch
- CIRCUIT_BREAKER (3x threshold): abort and return partial result

Integrates as a check in the ReAct loop (should_continue_after_model).
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field

log = logging.getLogger("kronos.security.loop_detector")

# Thresholds
WARN_THRESHOLD = 10
CRITICAL_THRESHOLD = 20
CIRCUIT_BREAKER_THRESHOLD = 30


class LoopLevel:
    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"
    CIRCUIT_BREAKER = "circuit_breaker"


@dataclass
class ToolCallRecord:
    name: str
    args_hash: str
    result_hash: str = ""


@dataclass
class LoopDetector:
    """Tracks tool calls within a single conversation turn and detects loops."""

    history: list[ToolCallRecord] = field(default_factory=list)
    _repeat_counts: dict[str, int] = field(default_factory=dict)

    def record(self, tool_name: str, tool_args: dict, tool_result: str = "") -> None:
        """Record a tool call."""
        args_hash = _hash_dict(tool_args)
        result_hash = _hash_str(tool_result) if tool_result else ""

        record = ToolCallRecord(
            name=tool_name,
            args_hash=args_hash,
            result_hash=result_hash,
        )
        self.history.append(record)

        # Track repeat counts
        call_key = f"{tool_name}:{args_hash}"
        self._repeat_counts[call_key] = self._repeat_counts.get(call_key, 0) + 1

    def check(self) -> tuple[str, str]:
        """Check for loops. Returns (level, description).

        Call after each tool execution to check if agent is looping.
        """
        if len(self.history) < 3:
            return LoopLevel.OK, ""

        # 1. Generic repeat: same tool + same args
        level, desc = self._check_generic_repeat()
        if level != LoopLevel.OK:
            return level, desc

        # 2. Ping-pong: alternating between two tools
        level, desc = self._check_ping_pong()
        if level != LoopLevel.OK:
            return level, desc

        # 3. Poll no progress: same tool, same result
        level, desc = self._check_poll_no_progress()
        if level != LoopLevel.OK:
            return level, desc

        return LoopLevel.OK, ""

    def _check_generic_repeat(self) -> tuple[str, str]:
        """Detect: same tool called with same args repeatedly."""
        for call_key, count in self._repeat_counts.items():
            tool_name = call_key.split(":")[0]
            if count >= CIRCUIT_BREAKER_THRESHOLD:
                return LoopLevel.CIRCUIT_BREAKER, f"Tool '{tool_name}' called {count} times with same args"
            if count >= CRITICAL_THRESHOLD:
                return LoopLevel.CRITICAL, f"Tool '{tool_name}' called {count} times with same args"
            if count >= WARN_THRESHOLD:
                return LoopLevel.WARNING, f"Tool '{tool_name}' called {count} times with same args"
        return LoopLevel.OK, ""

    def _check_ping_pong(self) -> tuple[str, str]:
        """Detect: alternating between two tools (A→B→A→B...)."""
        if len(self.history) < 6:
            return LoopLevel.OK, ""

        recent = self.history[-20:]  # check last 20 calls
        if len(recent) < 6:
            return LoopLevel.OK, ""

        # Check if last N calls alternate between exactly 2 tools
        names = [r.name for r in recent]
        unique = set(names)
        if len(unique) != 2:
            return LoopLevel.OK, ""

        # Check alternating pattern
        alternating = 0
        for i in range(1, len(names)):
            if names[i] != names[i - 1]:
                alternating += 1

        # If >80% are alternating, it's ping-pong
        if alternating / (len(names) - 1) > 0.8:
            tools = list(unique)
            count = len(names)
            if count >= CIRCUIT_BREAKER_THRESHOLD:
                return LoopLevel.CIRCUIT_BREAKER, f"Ping-pong between '{tools[0]}' and '{tools[1]}' ({count} calls)"
            if count >= CRITICAL_THRESHOLD:
                return LoopLevel.CRITICAL, f"Ping-pong between '{tools[0]}' and '{tools[1]}' ({count} calls)"
            if count >= WARN_THRESHOLD:
                return LoopLevel.WARNING, f"Ping-pong between '{tools[0]}' and '{tools[1]}' ({count} calls)"

        return LoopLevel.OK, ""

    def _check_poll_no_progress(self) -> tuple[str, str]:
        """Detect: same tool, same result (polling without change)."""
        if len(self.history) < 4:
            return LoopLevel.OK, ""

        # Check recent calls for same tool + same result
        recent = [r for r in self.history[-20:] if r.result_hash]
        if len(recent) < 4:
            return LoopLevel.OK, ""

        # Group by tool name
        by_tool: dict[str, list[str]] = {}
        for r in recent:
            by_tool.setdefault(r.name, []).append(r.result_hash)

        for tool_name, hashes in by_tool.items():
            if len(hashes) < 4:
                continue
            # Check if last N results are identical
            unique_results = set(hashes[-10:])
            if len(unique_results) == 1 and len(hashes) >= WARN_THRESHOLD:
                count = len(hashes)
                if count >= CIRCUIT_BREAKER_THRESHOLD:
                    return LoopLevel.CIRCUIT_BREAKER, f"'{tool_name}' returns same result {count} times (no progress)"
                if count >= CRITICAL_THRESHOLD:
                    return LoopLevel.CRITICAL, f"'{tool_name}' returns same result {count} times (no progress)"
                return LoopLevel.WARNING, f"'{tool_name}' returns same result {count} times (no progress)"

        return LoopLevel.OK, ""

    def reset(self) -> None:
        """Reset detector for a new conversation turn."""
        self.history.clear()
        self._repeat_counts.clear()


def get_nudge_message(level: str, description: str) -> str:
    """Generate a system message to inject when loop is detected."""
    if level == LoopLevel.WARNING:
        return (
            f"[LOOP DETECTED — WARNING] {description}. "
            "Ты повторяешь одни и те же действия. Попробуй другой подход: "
            "используй другой tool, измени параметры, или сформулируй ответ "
            "на основе уже полученных данных."
        )
    elif level == LoopLevel.CRITICAL:
        return (
            f"[LOOP DETECTED — CRITICAL] {description}. "
            "СТОП. Ты застрял в цикле. НЕ вызывай этот tool снова. "
            "Дай ответ пользователю на основе того, что уже знаешь. "
            "Если задача невыполнима — скажи об этом прямо."
        )
    elif level == LoopLevel.CIRCUIT_BREAKER:
        return (
            f"[CIRCUIT BREAKER] {description}. "
            "Выполнение прервано из-за зацикливания."
        )
    return ""


def _hash_dict(d: dict) -> str:
    """Deterministic hash of a dict."""
    serialized = json.dumps(d, sort_keys=True, default=str)
    return hashlib.md5(serialized.encode()).hexdigest()[:12]


def _hash_str(s: str) -> str:
    """Hash of a string (for result comparison)."""
    return hashlib.md5(s.encode()).hexdigest()[:12]
