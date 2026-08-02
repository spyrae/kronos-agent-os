"""Group chat routing — each agent independently decides whether to respond.

Architecture: 6 agents as separate processes, all receive every group message.
Each agent runs this router to decide: should I respond? With what delay?

Tier 1: Explicit addressing (1-5s delay)
  - @mention of my username, reply to my message

Tier 2: Topic relevance (5-20s delay, user messages only)
  - Owner-first: if the message is about a topic this agent `owns`, it answers
    without the relevance check and on the fast lane, so it wins arbitration
    against agents who merely find the topic interesting
  - Otherwise LLM quick-check: is this my domain? Score 1-10, respond if ≥7
  - A non-owner who would answer an owned topic waits, giving the owner a head
    start; if the owner is down, the deferred reply still lands
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

Two kinds of "topic" meet here, and they are not the same thing. A Telegram
forum topic is routed by id in `bridge_topics` (`TOPIC_*` + `*_agent`): a hard
assignment where non-owners never even reach this router. What `owns` in
agents.yaml describes is a *subject* — "planning", "metrics" — recognised from
the message itself, which is what the shared stream needs, because there every
agent sees every message.
"""

import hashlib
import logging
import random
import re
import time
from dataclasses import dataclass, field

log = logging.getLogger("kronos.group_router")

# How many peer bot replies before this agent skips (Tier 2 post-delay check
# and Tier 3 pre-send check). Keeps chat volume bounded.
MAX_PEER_REPLIES = 2

# Tier 3: cooldown between peer reactions per agent (seconds).
PEER_REACTION_COOLDOWN = 300  # 5 minutes

# A peer replying to my message reads as an explicit address, and my answer is
# itself a reply to them — which is how two agents ping-pong forever. Every
# other loop guard is bypassed at Tier 1 by design, because Tier 1 exists so the
# *user's* explicit address is always honoured; a peer's does not earn that.
#
# Two bounds, and they are not redundant:
#
#   * MAX_AGENT_REPLIES_PER_ROOT is the exact one — a ceiling on everything the
#     swarm says about one user message, counted in the shared ledger, so it
#     binds across agents and across process restarts. A normal exchange is
#     1-3 replies; a loop hits the ceiling and stops.
#   * MAX_PEER_EXCHANGES per window is the fallback. Ledger reads fail open
#     (a locked database must not mute an agent), so without a local bound a
#     ledger outage would restore the unbounded loop.
PEER_EXCHANGE_WINDOW = 600  # 10 minutes
MAX_PEER_EXCHANGES = 2
MAX_AGENT_REPLIES_PER_ROOT = 6

# Topic classification is agent-independent and messages repeat (edits, retries),
# so the lite-tier answer is cached per process for five minutes.
TOPIC_CACHE_TTL = 300

# How long a non-owner defers to the topic owner. Bounded below the swarm's
# CLAIM_EXPIRY_SECONDS on purpose: a claim held longer than that expires and can
# never win arbitration, so a literal SLA-long sleep would mean "never answer"
# while still costing a task and an LLM call. Within this window a live owner
# always wins (their eta is seconds away); past it, an owner whose process is
# down no longer leaves the user waiting for the escalation job.
OWNER_DEFERENCE_SECONDS = 90

# Agent profiles loaded from agents.yaml (see agents.example.yaml for format).
# Usernames can be overridden per-agent via env: AGENT_USERNAME_KRONOS=..., etc.


def _load_profiles() -> dict[str, dict]:
    """Load agent profiles from agents.yaml, apply env overrides.

    Parsing and validation live in `kronos.swarm_config`; the router keeps the
    plain-dict shape because tools index it by key and tests replace it
    wholesale. `swarm_config.profile_for()` gives the typed view of an entry
    when the extended fields (ownership, SLA, budget) matter.
    """
    from kronos.swarm_config import load_profiles

    return {name: profile.model_dump() for name, profile in load_profiles().items()}


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
    # Recognised subject and its owner, when ownership is configured. The
    # transport uses them to register the SLA watch — including on a skip, so a
    # topic stays watched even when this agent has nothing to say about it.
    topic: str = ""
    topic_owner: str = ""
    owner_sla_minutes: int = 0


@dataclass
class EventFacts:
    """Everything the router reads from an incoming message.

    The one place that knows about Telethon's shape. Extracting it means the
    in-process swarm bus (and eval scenarios) can drive the real routing logic
    with a plain object instead of a mock that has to imitate a Telethon event —
    the tier and arbitration rules below are then provably the same in both.
    """

    text: str = ""
    sender_id: int = 0
    msg_id: int = 0
    # Where the message lives, so the router can ask the ledger about the
    # exchange this message belongs to.
    chat_id: int = 0
    topic_id: int = 0
    is_reply: bool = False
    reply_sender_id: int | None = None
    mentioned_usernames: set[str] = field(default_factory=set)
    mentioned_user_ids: set[int] = field(default_factory=set)
    topic_label: str = ""


def _topic_id_of(event) -> int:
    """Forum topic id, or 0. Same extraction the transport uses for the ledger."""
    try:
        from kronos.bridge_topics import _extract_topic_id_from_message

        return int(_extract_topic_id_from_message(getattr(event, "message", None), is_private=False) or 0)
    except Exception:
        return 0


async def event_facts(event) -> EventFacts:
    """Read an event once: Telethon's, the local bus's, or a test's stub."""
    if isinstance(event, EventFacts):
        return event

    facts = EventFacts(
        text=event.raw_text or "",
        sender_id=getattr(event, "sender_id", 0) or 0,
        msg_id=getattr(getattr(event, "message", None), "id", 0) or 0,
        chat_id=int(getattr(event, "chat_id", 0) or 0),
        topic_id=_topic_id_of(event),
        is_reply=bool(getattr(event, "is_reply", False)),
        topic_label=str(getattr(event, "topic_label", "") or ""),
    )

    from telethon.tl.types import MessageEntityMention, MessageEntityMentionName

    entities = getattr(getattr(event, "message", None), "entities", None) or []
    for ent in entities:
        if isinstance(ent, MessageEntityMentionName):
            facts.mentioned_user_ids.add(ent.user_id)
        elif isinstance(ent, MessageEntityMention):
            raw = facts.text[ent.offset : ent.offset + ent.length]
            facts.mentioned_usernames.add(raw.lstrip("@").lower())

    if facts.is_reply:
        try:
            replied = await event.get_reply_message()
        except Exception:
            replied = None
        if replied is not None:
            facts.reply_sender_id = getattr(replied, "sender_id", None)

    return facts


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
        swarm=None,
    ):
        # Explicit ledger beats the process singleton: the local bus and tests
        # hold their own store, and a router silently reading a different
        # database would report an empty exchange and never reach its ceiling.
        self._swarm = swarm
        self.agent_name = agent_name
        self.my_id = my_id
        self.my_username = (my_username or "").lower().lstrip("@")
        self.allowed_user_ids = allowed_user_ids

        profile = AGENT_PROFILES.get(
            agent_name,
            {"username": self.my_username, "aliases": [agent_name], "role": agent_name},
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

        # Tier 3: track peer reactions to prevent loops / flood.
        # -inf, not 0: the cooldown compares against time.monotonic(), which on
        # Linux counts from boot. With 0, a fresh process whose monotonic clock
        # is still < PEER_REACTION_COOLDOWN (e.g. right after a restart) would
        # falsely treat the FIRST reaction as cooled-down and never react.
        self._last_peer_reaction: float = float("-inf")
        self._reacted_to_msgs: set[int] = set()
        # Monotonic timestamps of Tier-1 replies to peers, for the exchange bound.
        self._peer_exchanges: list[float] = []

        # Ownership map, read once like the alias index above. An empty map is
        # the common case for a registry without `owns`, and it makes every
        # ownership branch below a no-op — including the classification call.
        from kronos.swarm_config import all_profiles

        self._profiles = all_profiles()
        self._topic_owners: dict[str, str] = {}
        for name, prof in self._profiles.items():
            for topic in prof.owns:
                # A contested topic has no single owner; swarm_config warned at
                # load time and routing falls back to plain relevance.
                self._topic_owners[topic] = "" if topic in self._topic_owners else name
        self._topic_owners = {topic: owner for topic, owner in self._topic_owners.items() if owner}
        self._topic_cache: dict[str, tuple[float, str]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def decide(self, event, client) -> RoutingDecision:
        """Main entry: should this agent respond to the group message?

        Accepts a Telethon event or an EventFacts — the local swarm bus passes
        the latter, so demos and eval scenarios exercise this exact logic.
        """
        facts = await event_facts(event)
        text = facts.text
        sender_id = facts.sender_id

        # Never respond to self
        if sender_id == self.my_id:
            return RoutingDecision(False, 0, 0, "own message")

        addressing = self._addressing_from_facts(facts)
        is_peer = self._is_peer(sender_id)

        # --- Cross-agent addressing guard (fires for both user and peer src) ---
        # If explicitly addressed to someone who is NOT me, always skip —
        # the addressed agent will respond via their own router.
        if addressing.explicit_to_other and not addressing.explicit_to_me:
            return RoutingDecision(
                False,
                0,
                0,
                f"addressed to {sorted(addressing.target_agents)}, not me",
                addressing=addressing,
            )

        # --- Peer bot messages ---
        if is_peer:
            # Tier 1: explicit @mention or reply from a peer → respond, but not
            # forever: my answer is a reply to them, which reads as an address
            # back to me on their side.
            if addressing.explicit_to_me:
                exhausted = self._exchange_budget_spent(facts)
                if exhausted:
                    return RoutingDecision(False, 0, 0, exhausted, addressing=addressing)
                self._peer_exchanges.append(time.monotonic())
                return RoutingDecision(
                    True,
                    random.uniform(3, 8),
                    1,
                    "peer @mentioned me",
                    addressing=addressing,
                )

            # Tier 3: auto-react if meaningfully disagree (with guards)
            msg_id = facts.msg_id

            # Guard 0: quiet mode — out of personal budget, so no volunteering.
            quiet = self._quiet_reason()
            if quiet:
                return RoutingDecision(False, 0, 0, f"quiet mode ({quiet})", addressing=addressing)

            # Guard 1: cooldown — max 1 peer reaction per 5 minutes
            now = time.monotonic()
            if now - self._last_peer_reaction < PEER_REACTION_COOLDOWN:
                return RoutingDecision(False, 0, 0, "peer cooldown active", addressing=addressing)

            # Guard 2: don't react to same message twice
            if msg_id in self._reacted_to_msgs:
                return RoutingDecision(False, 0, 0, "already reacted", addressing=addressing)

            # Guard 3: Tier 3 requires a user-root. Peer-to-peer chains do
            # not trigger reactions (otherwise bots debate each other forever).
            if not self._facts_reply_to_user(facts):
                return RoutingDecision(
                    False,
                    0,
                    0,
                    "peer not replying to a user message",
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
                True,
                random.uniform(20, 45),
                3,
                "disagree with peer",
                addressing=addressing,
            )

        # --- User messages ---

        # Tier 1: Explicit addressing → respond quickly
        if addressing.explicit_to_me:
            return RoutingDecision(
                True,
                random.uniform(1, 3),
                1,
                "explicit @me",
                addressing=addressing,
            )
        if addressing.reply_to_me:
            return RoutingDecision(
                True,
                random.uniform(2, 5),
                1,
                "reply to me",
                addressing=addressing,
            )

        # Quiet mode: an agent that spent its personal budget still answers when
        # addressed (Tier 1, above) but stops volunteering. Checked before the
        # classification and relevance calls, because those are the spend.
        quiet = self._quiet_reason()
        if quiet:
            return RoutingDecision(False, 0, 0, f"quiet mode ({quiet})", addressing=addressing)

        # Ownership: recognise the subject before spending a relevance call.
        topic = await self._topic_key(facts, text)
        owner = self._topic_owners.get(topic, "")
        owner_sla = self._profiles[owner].sla_minutes if owner in self._profiles else 0

        # Tier 2a: the owner answers its own topic — no relevance threshold, and
        # on the fast lane so it outranks non-owners in arbitration by eta.
        if owner and owner == self.agent_name:
            return RoutingDecision(
                True,
                random.uniform(1, 4),
                2,
                f"owner of '{topic}'",
                addressing=addressing,
                topic=topic,
                topic_owner=owner,
                owner_sla_minutes=owner_sla,
            )

        # Tier 2b: relevance as before.
        relevance = await self._check_relevance(text)
        if relevance >= 7:
            delay = random.uniform(5, 20)
            reason = f"relevance={relevance}"
            if owner:
                # Defer to the owner rather than answering over them.
                delay = OWNER_DEFERENCE_SECONDS
                reason = f"relevance={relevance}, deferring to owner {owner} of '{topic}'"
            return RoutingDecision(
                True,
                delay,
                2,
                reason,
                addressing=addressing,
                topic=topic,
                topic_owner=owner,
                owner_sla_minutes=owner_sla,
            )

        return RoutingDecision(
            False,
            0,
            0,
            f"low relevance={relevance}",
            addressing=addressing,
            topic=topic,
            topic_owner=owner,
            owner_sla_minutes=owner_sla,
        )

    @property
    def max_implicit_replies(self) -> int:
        """How many peer replies this agent tolerates before standing down.

        Per-agent override from agents.yaml, so a chatty generalist can be told
        to yield sooner than the swarm default without changing anyone else.
        """
        mine = self._profiles[self.agent_name].max_implicit_replies if self.agent_name in self._profiles else None
        return MAX_PEER_REPLIES if mine is None else mine

    def _peer_exchange_allowed(self) -> bool:
        """Room left in this window for another Tier-1 reply to a peer."""
        cutoff = time.monotonic() - PEER_EXCHANGE_WINDOW
        self._peer_exchanges = [ts for ts in self._peer_exchanges if ts > cutoff]
        return len(self._peer_exchanges) < MAX_PEER_EXCHANGES

    def _exchange_budget_spent(self, facts: "EventFacts") -> str:
        """Why this peer exchange must stop, or "" while it may continue.

        The ledger answer is the real one: it counts what the whole swarm said
        about the user message this chain descends from, so it binds across
        agents and survives a restart. The per-process window is checked too,
        because the ledger read fails open and something has to hold if it does.
        """
        replies = self._replies_to_root(facts)
        if replies >= MAX_AGENT_REPLIES_PER_ROOT:
            return f"exchange ceiling reached ({replies} agent replies to this user message)"
        if not self._peer_exchange_allowed():
            return f"peer exchange budget spent ({MAX_PEER_EXCHANGES} per {PEER_EXCHANGE_WINDOW}s)"
        return ""

    def _replies_to_root(self, facts: "EventFacts") -> int:
        """How much the swarm has already said about this exchange's user root."""
        if not facts.chat_id:
            return 0
        try:
            from kronos.swarm_store import get_swarm

            swarm = self._swarm or get_swarm()
            root = swarm.resolve_user_root(
                chat_id=facts.chat_id,
                topic_id=facts.topic_id,
                msg_id=facts.msg_id,
            )
            return swarm.count_replies_to_root(
                chat_id=facts.chat_id,
                topic_id=facts.topic_id,
                root_msg_id=root,
            )
        except Exception as e:
            # Fail open: an unreadable ledger must not mute an agent. The window
            # bound above is what keeps a loop finite while this is broken.
            log.debug("[GroupRouter] Could not measure the exchange: %s", e)
            return 0

    def _quiet_reason(self) -> str:
        """Non-empty when this agent has spent its personal daily budget."""
        try:
            from kronos.security.cost_guardian import get_guardian

            return get_guardian().quiet_reason(self.agent_name)
        except Exception as e:  # pragma: no cover - defensive
            # A budget read must never be the reason an agent goes mute.
            log.debug("[GroupRouter] Quiet-mode check failed: %s", e)
            return ""

    async def should_still_respond(self, event, client, tier: int) -> bool:
        """Re-check after delay: did too many peers already respond?

        Applies to both Tier 2 and Tier 3 now (Tier 1 explicit @mention is
        always honored). Tier 1 messages intentionally bypass this check.
        """
        if tier == 1:
            return True
        count = await self._count_peer_replies(event, client)
        if count >= self.max_implicit_replies:
            log.info(
                "[GroupRouter] %s: %d peers already replied (tier=%d), skipping",
                self.agent_name,
                count,
                tier,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Addressing analysis (Tier 1 precursor + cross-agent guard)
    # ------------------------------------------------------------------

    async def _analyze_addressing(self, event, text: str) -> AddressingInfo:
        """Kept for callers that hold an event; the logic lives on facts."""
        return self._addressing_from_facts(await event_facts(event))

    def _addressing_from_facts(self, facts: "EventFacts") -> AddressingInfo:
        info = AddressingInfo()
        text_lower = facts.text.lower()

        # 1. Telegram mention entities — authoritative for @username and
        #    MentionName (explicit user_id resolution).
        if self.my_id in facts.mentioned_user_ids:
            info.explicit_to_me = True
            info.target_agents.add(self.agent_name)
        for uname in facts.mentioned_usernames:
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
        if facts.is_reply and facts.reply_sender_id == self.my_id:
            info.reply_to_me = True
            info.explicit_to_me = True
            info.target_agents.add(self.agent_name)

        info.explicit_to_other = bool(info.target_agents) and self.agent_name not in info.target_agents
        return info

    # ------------------------------------------------------------------
    # Tier 3 guard: peer must reply to a user-root message
    # ------------------------------------------------------------------

    def _facts_reply_to_user(self, facts: EventFacts) -> bool:
        """True if this peer message is a reply to a whitelisted user.

        A peer message with no reply linkage is NOT treated as user-rooted. That
        is deliberately strict: it prevents bots from reacting to each other
        without a user anchor.
        """
        return facts.is_reply and facts.reply_sender_id in self.allowed_user_ids

    async def _peer_replies_to_user(self, event) -> bool:
        """Event-level wrapper around the fact check above."""
        return self._facts_reply_to_user(await event_facts(event))

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
    # Ownership: which declared subject is this message about?
    # ------------------------------------------------------------------

    async def _topic_key(self, event, text: str) -> str:
        """One of the topics declared in `owns`, or "" when none applies.

        Free when the registry declares no ownership, which keeps the whole
        feature invisible to swarms that do not use it. Takes an EventFacts or
        anything with a `topic_label`.
        """
        if not self._topic_owners:
            return ""

        # A message may name its subject directly (the local swarm bus and eval
        # scenarios do). Trust it when it matches a declared topic — no LLM.
        declared = str(getattr(event, "topic_label", "") or "").strip().lower()
        if declared in self._topic_owners:
            return declared

        return await self._classify_topic(text)

    async def _classify_topic(self, text: str) -> str:
        """Lite-tier classification of the message into a declared topic."""
        if not text.strip():
            return ""

        topics = sorted(self._topic_owners)
        cache_key = hashlib.sha256("|".join([text[:500], *topics]).encode("utf-8")).hexdigest()
        cached = self._topic_cache.get(cache_key)
        now = time.monotonic()
        if cached and cached[0] > now:
            return cached[1]

        from langchain_core.messages import HumanMessage

        from kronos.llm import ModelTier, get_model

        prompt = (
            "Classify the message into exactly one of these topics:\n"
            f"{', '.join(topics)}\n"
            "Reply with ONLY the topic label, or NONE if none of them fits.\n\n"
            f"Message: {text[:500]}"
        )

        try:
            model = get_model(ModelTier.LITE)
            response = await model.ainvoke([HumanMessage(content=prompt)])
            raw = response.content if isinstance(response.content, str) else str(response.content)
            answer = raw.strip().strip(".\"'").lower()
            # The model can invent a label; only a declared one counts.
            topic = answer if answer in self._topic_owners else ""
        except Exception as e:
            # Fail open: a classification glitch must not silence the swarm.
            log.warning("[GroupRouter] Topic classification failed: %s", e)
            return ""

        if len(self._topic_cache) > 500:
            self._topic_cache.clear()
        self._topic_cache[cache_key] = (now + TOPIC_CACHE_TTL, topic)
        return topic

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
            f'Another team member just said:\n"{text[:500]}"\n\n'
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
