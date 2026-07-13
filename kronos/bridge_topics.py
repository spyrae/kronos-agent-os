"""Telegram forum-topic routing helpers for the bridge.

Pure functions (no live-client / agent state) extracted from ``bridge.py``:
supergroup id normalization, thread-id parsing, and the swarm/owner/silent
topic-routing decision. ``bridge.py`` re-exports every name here, so
``kronos.bridge.<name>`` keeps resolving unchanged.
"""

import os
from dataclasses import dataclass

from kronos.config import settings


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
