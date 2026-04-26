"""Group chat routing — each agent independently decides whether to respond.

Architecture: 6 agents as separate processes, all receive every group message.
Each agent runs this router to decide: should I respond? With what delay?

Tier 1: Explicit addressing (1-5s delay)
  - @mention of my username, reply to my message

Tier 2: Topic relevance (5-20s delay, user messages only)
  - LLM quick-check: is this my domain? Score 1-10, respond if ≥7
  - Skipped entirely when another known agent is addressed
  - After delay: check if ≥MAX_PEER_REPLIES peers already replied → skip

Tier 3: Peer reaction (15-45s delay, bot messages only)
  - Another bot replied to a user message → LLM: do I meaningfully disagree?
  - Requires a user-root message (ignores peer→peer chains)
  - Skipped when an agent is explicitly addressed

Cross-agent addressing guard
  - If the user @-addresses specific known agents (by username or alias),
    only those agents pass Tier 1; everyone else skips silently. This is
    the guard that fixes "Impulse answers when Nexus was addressed".
"""

import logging
import os
import random
import re
import time
from dataclasses import dataclass, field

import yaml

log = logging.getLogger("kronos.group_router")

# How many peer bot replies before this agent skips (Tier 2 post-delay check
# and Tier 3 pre-send check). Keeps chat volume bounded.
MAX_PEER_REPLIES = 2

# Tier 3: cooldown between peer reactions per agent (seconds).
PEER_REACTION_COOLDOWN = 300  # 5 minutes

# Agent profiles loaded from agents.yaml (see agents.example.yaml for format).
# Usernames can be overridden per-agent via env: AGENT_USERNAME_KRONOS=..., etc.


def _load_profiles() -> dict[str, dict]:
    """Load agent profiles from agents.yaml, apply env overrides."""
    config_path = os.environ.get(
        "AGENTS_CONFIG_PATH",
        os.path.join(os.path.dirname(__file__), "..", "agents.yaml"),
    )
    config_path = os.path.normpath(config_path)

    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    else:
        log.warning("agents.yaml not found at %s — using empty profile set", config_path)
        raw = {}

    resolved: dict[str, dict] = {}
    for name, base in raw.items():
        username = os.environ.get(
            f"AGENT_USERNAME_{name.upper()}",
            base.get("username", f"{name}agnt"),
        )
        resolved[name] = {
            "username": username.lower().lstrip("@"),
            "aliases": [a.lower() for a in base.get("aliases", [name])],
            "role": base.get("role", ""),
        }
    return resolved


AGENT_PROFILES: dict[str, dict] = _load_profiles()


@dataclass
class AddressingInfo:
    """What the router learned about who this message is for.

    target_agents: set of agent_name values explicitly addressed via @username
                   or natural-language aliases. Empty set means "not addressed
                   to anyone in particular".
    explicit_to_me: this agent's @username or alias appears in the text.
    explicit_to_other: target_agents is non-empty and I am not in it.
    reply_to_me: Telegram reply targeting a message I sent.
    """

    target_agents: set[str] = field(default_factory=set)
    explicit_to_me: bool = False
    explicit_to_other: bool = False
    reply_to_me: bool = False


@dataclass
class RoutingDecision:
    should_respond: bool
    delay: float  # seconds to wait before responding
    tier: int  # 0=skip, 1=explicit, 2=relevance, 3=peer-reaction
    reason: str = ""
    addressing: AddressingInfo | None = None


# Word-boundary alias matching — stops "импульс" substring from firing on
# unrelated words. Accepts letters/numbers/underscore on either side as
# non-match, unicode-aware via re.UNICODE (default in py3).
def _alias_in_text(alias: str, text_lower: str) -> bool:
    pattern = r"(?:^|[^\w])" + re.escape(alias) + r"(?:$|[^\w])"
    return re.search(pattern, text_lower) is not None


class GroupRouter:
    """Decides whether this agent should respond to a group message."""

    def __init__(
        self,
        agent_name: str,
        my_id: int,
        my_username: str | None,
        allowed_user_ids: set[int],
    ):
        self.agent_name = agent_name
        self.my_id = my_id
        self.my_username = (my_username or "").lower().lstrip("@")
        self.allowed_user_ids = allowed_user_ids

        profile = AGENT_PROFILES.get(
            agent_name, {"username": self.my_username, "aliases": [agent_name], "role": agent_name},
        )
        self.aliases: list[str] = profile["aliases"]
        self.role: str = profile["role"]

        # If Telethon login gave us a real username, use it — otherwise fall
        # back to the profile's default. Either way, keep it in sync with
        # what other agents will look for.
        if not self.my_username:
            self.my_username = profile["username"]

        # Build reverse index: username/alias → canonical agent_name
        # Used to detect "this message addresses some agent, which one?".
        self._username_to_agent: dict[str, str] = {}
        self._alias_to_agent: dict[str, str] = {}
        for name, data in AGENT_PROFILES.items():
            self._username_to_agent[data["username"]] = name
            for alias in data["aliases"]:
                self._alias_to_agent[alias] = name

        # Tier 3: track peer reactions to prevent loops / flood
        self._last_peer_reaction: float = 0
        self._reacted_to_msgs: set[int] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def decide(self, event, client) -> RoutingDecision:
        """Main entry: should this agent respond to the group message?"""
        text = event.raw_text or ""
        sender_id = event.sender_id

        # Never respond to self
        if sender_id == self.my_id:
            return RoutingDecision(False, 0, 0, "own message")

        addressing = await self._analyze_addressing(event, text)
        is_peer = self._is_peer(sender_id)

        # --- Cross-agent addressing guard (fires for both user and peer src) ---
        # If explicitly addressed to someone who is NOT me, always skip —
        # the addressed agent will respond via their own router.
        if addressing.explicit_to_other and not addressing.explicit_to_me:
            return RoutingDecision(
                False, 0, 0,
                f"addressed to {sorted(addressing.target_agents)}, not me",
                addressing=addressing,
            )

        # --- Peer bot messages ---
        if is_peer:
            # Tier 1: explicit @mention from peer → always respond
            if addressing.explicit_to_me:
                return RoutingDecision(
                    True, random.uniform(3, 8), 1, "peer @mentioned me",
                    addressing=addressing,
                )

            # Tier 3: auto-react if meaningfully disagree (with guards)
            msg_id = event.message.id

            # Guard 1: cooldown — max 1 peer reaction per 5 minutes
            now = time.monotonic()
            if now - self._last_peer_reaction < PEER_REACTION_COOLDOWN:
                return RoutingDecision(False, 0, 0, "peer cooldown active", addressing=addressing)

            # Guard 2: don't react to same message twice
            if msg_id in self._reacted_to_msgs:
                return RoutingDecision(False, 0, 0, "already reacted", addressing=addressing)

            # Guard 3: Tier 3 requires a user-root. Peer-to-peer chains do
            # not trigger reactions (otherwise bots debate each other forever).
            replied_to_user = await self._peer_replies_to_user(event)
            if not replied_to_user:
                return RoutingDecision(
                    False, 0, 0, "peer not replying to a user message",
                    addressing=addressing,
                )

            should = await self._should_react_to_peer(text)
            if not should:
                return RoutingDecision(False, 0, 0, "agree with peer / not my area", addressing=addressing)

            self._last_peer_reaction = now
            self._reacted_to_msgs.add(msg_id)
            if len(self._reacted_to_msgs) > 100:
                self._reacted_to_msgs.clear()
            return RoutingDecision(
                True, random.uniform(20, 45), 3, "disagree with peer",
                addressing=addressing,
            )

        # --- User messages ---

        # Tier 1: Explicit addressing → respond quickly
        if addressing.explicit_to_me:
            return RoutingDecision(
                True, random.uniform(1, 3), 1, "explicit @me",
                addressing=addressing,
            )
        if addressing.reply_to_me:
            return RoutingDecision(
                True, random.uniform(2, 5), 1, "reply to me",
                addressing=addressing,
            )

        # Tier 2: Topic relevance → respond with delay
        relevance = await self._check_relevance(text)
        if relevance >= 7:
            return RoutingDecision(
                True, random.uniform(5, 20), 2, f"relevance={relevance}",
                addressing=addressing,
            )

        return RoutingDecision(
            False, 0, 0, f"low relevance={relevance}", addressing=addressing,
        )

    async def should_still_respond(self, event, client, tier: int) -> bool:
        """Re-check after delay: did too many peers already respond?

        Applies to both Tier 2 and Tier 3 now (Tier 1 explicit @mention is
        always honored). Tier 1 messages intentionally bypass this check.
        """
        if tier == 1:
            return True
        count = await self._count_peer_replies(event, client)
        if count >= MAX_PEER_REPLIES:
            log.info(
                "[GroupRouter] %s: %d peers already replied (tier=%d), skipping",
                self.agent_name, count, tier,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Addressing analysis (Tier 1 precursor + cross-agent guard)
    # ------------------------------------------------------------------

    async def _analyze_addressing(self, event, text: str) -> AddressingInfo:
        info = AddressingInfo()
        text_lower = text.lower()

        # 1. Telegram mention entities — authoritative for @username and
        #    MentionName (explicit user_id resolution).
        from telethon.tl.types import MessageEntityMention, MessageEntityMentionName
        entities = event.message.entities or []
        for ent in entities:
            if isinstance(ent, MessageEntityMentionName):
                if ent.user_id == self.my_id:
                    info.explicit_to_me = True
                    info.target_agents.add(self.agent_name)
            elif isinstance(ent, MessageEntityMention):
                raw = event.raw_text[ent.offset : ent.offset + ent.length]
                uname = raw.lstrip("@").lower()
                if uname == self.my_username:
                    info.explicit_to_me = True
                    info.target_agents.add(self.agent_name)
                elif uname in self._username_to_agent:
                    info.target_agents.add(self._username_to_agent[uname])

        # 2. Fallback: raw-text @username scan (covers cases where the
        #    message came through a path without entities).
        for uname, agent_name in self._username_to_agent.items():
            if f"@{uname}" in text_lower:
                info.target_agents.add(agent_name)
                if uname == self.my_username:
                    info.explicit_to_me = True

        # 3. Natural-language alias matching (word-boundary) for any known agent.
        for alias, agent_name in self._alias_to_agent.items():
            if _alias_in_text(alias, text_lower):
                info.target_agents.add(agent_name)
                if agent_name == self.agent_name:
                    info.explicit_to_me = True

        # 4. Reply-to-me
        if event.is_reply:
            try:
                replied = await event.get_reply_message()
                if replied is not None and replied.sender_id == self.my_id:
                    info.reply_to_me = True
                    info.explicit_to_me = True
                    info.target_agents.add(self.agent_name)
            except Exception:
                pass

        info.explicit_to_other = bool(info.target_agents) and self.agent_name not in info.target_agents
        return info

    # ------------------------------------------------------------------
    # Tier 3 guard: peer must reply to a user-root message
    # ------------------------------------------------------------------

    async def _peer_replies_to_user(self, event) -> bool:
        """True if this peer message is a reply to a user message.

        Also accepts the case where the peer message is standalone (no reply)
        only if it appeared right after a user message — we approximate that
        by saying: if there's no reply linkage at all, treat as NOT user-rooted.
        This is stricter than before and intentionally so; it prevents bots
        from reacting to each other without a user anchor.
        """
        if not event.is_reply:
            return False
        try:
            replied = await event.get_reply_message()
            if replied is None:
                return False
            # If the message being replied to is from a whitelisted user,
            # this is a user-rooted thread.
            return replied.sender_id in self.allowed_user_ids
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Sender classification
    # ------------------------------------------------------------------

    def _is_user(self, sender_id: int) -> bool:
        return sender_id in self.allowed_user_ids

    def _is_peer(self, sender_id: int) -> bool:
        return sender_id != self.my_id and sender_id not in self.allowed_user_ids

    # ------------------------------------------------------------------
    # Tier 2: Relevance check (LLM, lite model)
    # ------------------------------------------------------------------

    async def _check_relevance(self, text: str) -> int:
        """Quick LLM check: how relevant is this message to my domain? 1-10."""
        from langchain_core.messages import HumanMessage

        from kronos.llm import ModelTier, get_model

        prompt = (
            f"You are a {self.role}.\n"
            f"Rate 1-10 how relevant this message is to YOUR specific expertise.\n"
            f"8-10: clearly your domain, you'd add unique value.\n"
            f"5-7: somewhat relevant but another specialist might be better.\n"
            f"1-4: not your area.\n"
            f"Reply with ONLY a single number.\n\n"
            f"Message: {text[:500]}"
        )

        try:
            model = get_model(ModelTier.LITE)
            response = await model.ainvoke([HumanMessage(content=prompt)])
            content = response.content.strip() if isinstance(response.content, str) else str(response.content)
            match = re.search(r"\d+", content)
            return min(int(match.group()), 10) if match else 5
        except Exception as e:
            log.warning("[GroupRouter] Relevance check failed: %s", e)
            return 5  # neutral — don't respond on error

    # ------------------------------------------------------------------
    # Tier 3: Peer reaction (LLM, lite model)
    # ------------------------------------------------------------------

    async def _should_react_to_peer(self, text: str) -> bool:
        """Should I add my perspective to another bot's message?

        Tightened prompt: explicit request for a *meaningfully different*
        perspective, not minor agreement. Still a lite-tier LLM call.
        """
        from langchain_core.messages import HumanMessage

        from kronos.llm import ModelTier, get_model

        prompt = (
            f"You are a {self.role}.\n"
            f"Another team member just said:\n\"{text[:500]}\"\n\n"
            f"Do you have a MEANINGFULLY DIFFERENT perspective that would "
            f"change the conclusion or surface critical missing context?\n"
            f"This is NOT about agreeing with nuance. Only say YES if skipping "
            f"your input would leave the user with a worse answer.\n"
            f"Reply ONLY: YES or NO"
        )

        try:
            model = get_model(ModelTier.LITE)
            response = await model.ainvoke([HumanMessage(content=prompt)])
            content = response.content.strip() if isinstance(response.content, str) else str(response.content)
            return content.lower().startswith("yes")
        except Exception as e:
            log.warning("[GroupRouter] Peer reaction check failed: %s", e)
            return False  # don't react on error

    # ------------------------------------------------------------------
    # Anti-flood: count peer replies to the same user message
    # ------------------------------------------------------------------

    async def _count_peer_replies(self, event, client) -> int:
        """Count peer bot replies to the root user message."""
        try:
            count = 0
            async for msg in client.iter_messages(
                event.chat_id,
                limit=20,
                min_id=event.message.id,
            ):
                reply_to = getattr(msg, "reply_to", None)
                if not reply_to:
                    continue
                reply_msg_id = getattr(reply_to, "reply_to_msg_id", None)
                if reply_msg_id == event.message.id and self._is_peer(msg.sender_id):
                    count += 1
            return count
        except Exception as e:
            log.warning("[GroupRouter] Count peer replies failed: %s", e)
            return 0
