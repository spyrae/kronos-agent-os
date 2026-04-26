"""LLM tier router — ported from Kronos I worker.cjs classifyTier()."""

from kronos.llm import ModelTier

SIMPLE_PATTERNS_RU = [
    "привет", "здравствуй", "добрый день", "добрый вечер", "доброе утро",
    "спасибо", "благодарю", "ок", "хорошо", "понял", "ясно", "круто",
    "пока", "до свидания", "удачи",
    "да", "нет", "ага", "не", "угу",
    "что нового", "как дела", "status",
]

COMPLEX_PATTERNS = [
    # Russian
    "анализ", "сравни", "стратеги", "разбери", "исследуй", "оцени",
    "план", "архитектур", "рефактор", "оптимиз",
    "инвестиц", "портфел", "рынк", "обзор",
    "напиши код", "реализуй", "объясни", "расскажи",
    "проверь", "найди", "посмотри", "сделай",
    # English
    "analyze", "review", "implement", "compare", "research", "evaluate",
    "strategy", "plan", "optimize", "refactor", "explain", "describe",
    "market", "invest", "trend", "write code",
    "check", "find", "look", "search",
    # System (cron task markers)
    "HEARTBEAT", "NEWS MONITOR", "SELF-IMPROVEMENT",
]


def classify_tier(message: str) -> ModelTier:
    """Classify message into lite or standard LLM tier."""
    length = len(message)
    lower = message.lower()

    if length < 30:
        return ModelTier.LITE

    if any(p.lower() in lower for p in COMPLEX_PATTERNS):
        return ModelTier.STANDARD

    if length < 100 and any(p in lower for p in SIMPLE_PATTERNS_RU):
        return ModelTier.LITE

    if length < 50:
        return ModelTier.LITE

    return ModelTier.STANDARD
