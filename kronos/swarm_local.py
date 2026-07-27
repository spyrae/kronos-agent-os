"""The swarm without Telegram (moat 11.5).

Coordination was physically tied to one transport: `GroupRouter.decide` took a
Telethon event and the pollers delivered through the bridge's webhook. That made
the most distinctive part of this system the least demonstrable — you could not
show arbitration, ownership or escalation without six Telegram accounts, and none
of it could go into an eval.

This module runs the same routing in one process against a real `swarm.db`. It is
a bus, not a second implementation: the tier rules, the ownership shortcut and the
claim arbitration are the production ones, reached through `EventFacts` (see
`group_router.event_facts`). What it deliberately does not reproduce:

* **Real delays.** Claims are ordered by their eta and resolved immediately, so a
  round is instant. Production sleeps.
* **The post-delay peer recount.** That reads chat history through a Telegram
  client; here the claim ledger is the only source of truth, which is the
  stricter of the two checks anyway.
* **Model calls.** Replies, relevance and peer reactions come from deterministic
  callbacks — a demo must not need provider keys.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from kronos.group_router import EventFacts, GroupRouter
from kronos.swarm_store import SwarmStore

log = logging.getLogger("kronos.swarm_local")

DEMO_CHAT_ID = -100_900_001
DEMO_TOPIC_ID = 0
USER_ID = 1

ReplyFn = Callable[[str, str], str]  # (agent_name, incoming text) -> reply
RelevanceFn = Callable[[str, str], int]  # (agent_name, incoming text) -> 1..10
ReactFn = Callable[[str, str], bool]  # (agent_name, peer text) -> react?


def _default_reply(agent: str, text: str) -> str:
    """Name-free on purpose.

    An agent's own alias inside a message body is read by the router as an
    address to that agent, so "[nexus] ..." in a reply would make every peer skip
    the thread as "addressed to nexus". Who spoke is in the transcript instead.
    """
    return f"Отвечаю по своей части: {text[:60]}"


def _default_relevance(agent: str, text: str) -> int:
    """Deterministic stand-in for the lite-tier relevance call."""
    return 8 if agent.lower() in text.lower() else 4


def _never_react(agent: str, text: str) -> bool:
    return False


@dataclass
class LocalAgent:
    """One participant: its router plus how it answers."""

    name: str
    router: GroupRouter
    reply: ReplyFn = _default_reply
    relevance: RelevanceFn = _default_relevance
    react: ReactFn = _never_react


@dataclass
class LocalSwarmBus:
    """In-process message bus over the real swarm ledger."""

    store: SwarmStore
    chat_id: int = DEMO_CHAT_ID
    topic_id: int = DEMO_TOPIC_ID
    agents: dict[str, LocalAgent] = field(default_factory=dict)
    transcript: list[dict] = field(default_factory=list)
    _next_msg_id: int = 1000
    _sender_ids: dict[str, int] = field(default_factory=dict)
    # msg_id → the user message this message belongs to (Tier 3 claims attach
    # to the root, exactly as the bridge resolves it from reply_to).
    _root_of: dict[int, int] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def add_agent(
        self,
        name: str,
        *,
        username: str = "",
        reply: ReplyFn | None = None,
        relevance: RelevanceFn | None = None,
        react: ReactFn | None = None,
    ) -> LocalAgent:
        """Register an agent. Its sender id comes from the join order."""
        sender_id = 2000 + len(self.agents)
        self._sender_ids[name] = sender_id
        router = GroupRouter(
            agent_name=name,
            my_id=sender_id,
            my_username=username or f"{name}agnt",
            allowed_user_ids={USER_ID},
        )
        agent = LocalAgent(
            name=name,
            router=router,
            reply=reply or _default_reply,
            relevance=relevance or _default_relevance,
            react=react or _never_react,
        )
        # Point the router's model hooks at the callbacks: a local round must be
        # deterministic and key-free. Topic recognition uses the message's label.
        router._check_relevance = _as_async(lambda text: agent.relevance(name, text))
        router._classify_topic = _as_async(lambda text: "")
        router._should_react_to_peer = _as_async(lambda text: agent.react(name, text))
        self.agents[name] = agent
        return agent

    # ------------------------------------------------------------------
    # Traffic
    # ------------------------------------------------------------------

    def post(self, agent: str | None, text: str, *, topic_label: str = "", reply_to: int | None = None) -> EventFacts:
        """Put a message on the bus. `agent=None` means the user."""
        self._next_msg_id += 1
        msg_id = self._next_msg_id
        sender_id = USER_ID if agent is None else self._sender_ids[agent]

        self.store.record_inbound_message(
            chat_id=self.chat_id,
            topic_id=self.topic_id,
            msg_id=msg_id,
            reply_to_msg_id=reply_to,
            sender_id=sender_id,
            sender_type="user" if agent is None else "agent",
            agent_name=agent,
            text=text,
        )
        self.transcript.append({"from": agent or "user", "text": text, "msg_id": msg_id})
        self._root_of[msg_id] = msg_id if agent is None else self._root_of.get(reply_to or 0, reply_to or msg_id)

        return EventFacts(
            text=text,
            sender_id=sender_id,
            msg_id=msg_id,
            is_reply=reply_to is not None,
            reply_sender_id=self._sender_of(reply_to),
            topic_label=topic_label,
        )

    async def run_round(self, facts: EventFacts) -> list[dict]:
        """Route one message through every agent; return what was actually sent.

        Mirrors the bridge: decide → claim → arbitrate → send, with the claims
        resolved in eta order instead of after a real sleep.
        """
        candidates: list[tuple] = []
        for agent in self.agents.values():
            if agent.router.my_id == facts.sender_id:
                continue

            decision = await agent.router.decide(facts, client=None)
            root_msg_id = self._root_msg_id(facts, decision.tier)

            if decision.topic_owner:
                self.store.watch_sla(
                    chat_id=self.chat_id,
                    topic_id=self.topic_id,
                    root_msg_id=root_msg_id,
                    thread_id=self._thread_id(),
                    topic=decision.topic,
                    owner_agent=decision.topic_owner,
                    request=facts.text,
                    sla_minutes=decision.owner_sla_minutes,
                )

            if not decision.should_respond:
                log.debug("[local] %s skips: %s", agent.name, decision.reason)
                continue

            self.store.claim_reply(
                chat_id=self.chat_id,
                topic_id=self.topic_id,
                root_msg_id=root_msg_id,
                trigger_msg_id=facts.msg_id,
                agent_name=agent.name,
                tier=decision.tier,
                eta_ts=decision.delay,  # relative: a round has no wall clock
                reason=decision.reason,
            )
            candidates.append((decision, agent, root_msg_id))

        sent: list[dict] = []
        # Earliest eta first — the order the real ledger arbitrates by, so the
        # owner's fast lane wins here for the same reason it wins in production.
        for decision, agent, root_msg_id in sorted(candidates, key=lambda item: item[0].delay):
            outcome = self.store.can_send_claim(
                chat_id=self.chat_id,
                topic_id=self.topic_id,
                root_msg_id=root_msg_id,
                agent_name=agent.name,
                tier=decision.tier,
                max_implicit_replies=agent.router.max_implicit_replies,
            )
            if not outcome.won:
                self.store.cancel_claim(
                    chat_id=self.chat_id,
                    topic_id=self.topic_id,
                    trigger_msg_id=facts.msg_id,
                    agent_name=agent.name,
                    reason=outcome.reason,
                )
                self.store.incr_metric("duplicate_replies_avoided")
                log.debug("[local] %s stands down: %s", agent.name, outcome.reason)
                continue

            text = agent.reply(agent.name, facts.text)
            reply_facts = self.post(agent.name, text, reply_to=facts.msg_id)
            self.store.mark_sent(
                chat_id=self.chat_id,
                topic_id=self.topic_id,
                trigger_msg_id=facts.msg_id,
                agent_name=agent.name,
                reply_msg_id=reply_facts.msg_id,
            )
            sent.append(
                {
                    "agent": agent.name,
                    "tier": decision.tier,
                    "reason": decision.reason,
                    "text": text,
                    "msg_id": reply_facts.msg_id,
                    "reply_facts": reply_facts,
                }
            )
        return sent

    async def user_says(self, text: str, *, topic_label: str = "") -> list[dict]:
        """Convenience: post as the user, then run the round."""
        return await self.run_round(self.post(None, text, topic_label=topic_label))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _thread_id(self) -> str:
        return f"{self.chat_id}:{self.topic_id}" if self.topic_id else str(self.chat_id)

    def _sender_of(self, msg_id: int | None) -> int | None:
        if msg_id is None:
            return None
        for entry in self.transcript:
            if entry["msg_id"] == msg_id:
                return USER_ID if entry["from"] == "user" else self._sender_ids.get(entry["from"])
        return None

    def _root_msg_id(self, facts: EventFacts, tier: int) -> int:
        """Tier 3 claims attach to the user message the peer replied to."""
        if tier == 3:
            return self._root_of.get(facts.msg_id, facts.msg_id)
        return facts.msg_id


def _as_async(fn):
    """Wrap a sync callback so it can stand in for the router's async hooks."""

    async def _call(*args, **kwargs):
        return fn(*args, **kwargs)

    return _call
