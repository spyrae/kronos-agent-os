"""Tests for kronos.group_router — addressing + tier routing.

Covers the core decisions that the swarm refactor fixed:
  * AddressingInfo correctly detects @username of THIS agent vs OTHER agents.
  * Cross-agent guard: if another known agent is addressed, we skip.
  * Natural-language aliases match at word boundaries only.
  * Tier 1 fires on explicit @me or reply-to-me.
  * Tier 2 runs the LLM relevance check only when no specific agent is targeted.
  * Tier 3 requires the peer message to be a direct reply to a whitelisted user.
  * should_still_respond treats Tier 1 as exempt and applies MAX_PEER_REPLIES
    for Tiers 2 and 3.

The LLM-backed helpers (`_check_relevance`, `_should_react_to_peer`) are
patched so tests don't hit Fireworks/DeepSeek.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kronos.group_router import (
    AGENT_PROFILES,
    MAX_PEER_REPLIES,
    GroupRouter,
    _alias_in_text,
)

# --- User ID conventions for tests --------------------------------------------
MY_ID = 1001
USER_ID = 42
PEER_ANALYST_ID = 2001
PEER_REVIEWER_ID = 2002
ALLOWED_USERS = {USER_ID}

AGENT_PROFILES.clear()
AGENT_PROFILES.update({
    "kronos": {
        "username": "kronosagnt",
        "aliases": ["kronos"],
        "role": "primary coordinator",
    },
    "analyst": {
        "username": "analystagnt",
        "aliases": ["analyst"],
        "role": "analysis and research",
    },
    "operator": {
        "username": "operatoragnt",
        "aliases": ["operator"],
        "role": "execution and momentum",
    },
    "reviewer": {
        "username": "revieweragnt",
        "aliases": ["reviewer"],
        "role": "risk review",
    },
    "creative": {
        "username": "creativeagnt",
        "aliases": ["creative"],
        "role": "creative direction",
    },
    "strategist": {
        "username": "strategistagnt",
        "aliases": ["strategist"],
        "role": "strategy",
    },
})


def _make_router(agent_name: str = "operator") -> GroupRouter:
    """Router under test defaults to Operator unless overridden."""
    username = AGENT_PROFILES[agent_name]["username"]
    return GroupRouter(
        agent_name=agent_name,
        my_id=MY_ID,
        my_username=username,
        allowed_user_ids=ALLOWED_USERS,
    )


def _make_event(
    *,
    text: str,
    sender_id: int,
    msg_id: int = 100,
    entities: list | None = None,
    reply_msg=None,
):
    """Minimal stub mimicking a Telethon NewMessage event."""
    event = MagicMock()
    event.raw_text = text
    event.sender_id = sender_id
    event.message = MagicMock()
    event.message.id = msg_id
    event.message.entities = entities or []
    event.is_reply = reply_msg is not None
    event.get_reply_message = AsyncMock(return_value=reply_msg)
    return event


# ------------------------------------------------------------------------------
# Word-boundary alias matching
# ------------------------------------------------------------------------------


class TestAliasMatcher:
    def test_exact_word_matches(self):
        assert _alias_in_text("kronos", "kronos, что думаешь?")

    def test_substring_in_longer_word_does_not_match(self):
        """Prevents 'импульс' from firing on 'импульсивный'."""
        assert not _alias_in_text("kronos", "microsoft kronoscope")
        assert not _alias_in_text("operator", "operatoragnt замолчал")

    def test_punctuation_is_a_boundary(self):
        assert _alias_in_text("analyst", "эй, analyst! слышишь?")


# ------------------------------------------------------------------------------
# AddressingInfo
# ------------------------------------------------------------------------------


class TestAddressingInfo:
    @pytest.mark.asyncio
    async def test_no_mention_returns_blank_info(self):
        router = _make_router("operator")
        event = _make_event(text="просто фраза без тегов", sender_id=USER_ID)
        info = await router._analyze_addressing(event, event.raw_text)
        assert info.explicit_to_me is False
        assert info.explicit_to_other is False
        assert info.target_agents == set()

    @pytest.mark.asyncio
    async def test_at_username_of_other_agent_marks_explicit_to_other(self):
        router = _make_router("operator")
        event = _make_event(
            text="@analystagnt а ты что думаешь?", sender_id=USER_ID,
        )
        info = await router._analyze_addressing(event, event.raw_text)
        assert info.explicit_to_me is False
        assert info.explicit_to_other is True
        assert info.target_agents == {"analyst"}

    @pytest.mark.asyncio
    async def test_at_username_of_self_marks_explicit_to_me(self):
        router = _make_router("operator")
        event = _make_event(
            text="@operatoragnt запусти-ка мозги", sender_id=USER_ID,
        )
        info = await router._analyze_addressing(event, event.raw_text)
        assert info.explicit_to_me is True
        assert info.explicit_to_other is False
        assert info.target_agents == {"operator"}

    @pytest.mark.asyncio
    async def test_natural_alias_matches_self(self):
        router = _make_router("kronos")
        event = _make_event(text="Kronos, дай оценку", sender_id=USER_ID)
        info = await router._analyze_addressing(event, event.raw_text)
        assert info.explicit_to_me is True
        assert "kronos" in info.target_agents

    @pytest.mark.asyncio
    async def test_reply_to_my_message_marks_reply_to_me(self):
        router = _make_router("operator")
        replied = MagicMock()
        replied.sender_id = MY_ID
        event = _make_event(text="ок", sender_id=USER_ID, reply_msg=replied)
        info = await router._analyze_addressing(event, event.raw_text)
        assert info.reply_to_me is True
        assert info.explicit_to_me is True

    @pytest.mark.asyncio
    async def test_multi_mention_lists_all_targets(self):
        router = _make_router("operator")
        event = _make_event(
            text="@kronosagnt и @analystagnt что думаете?",
            sender_id=USER_ID,
        )
        info = await router._analyze_addressing(event, event.raw_text)
        assert info.target_agents == {"kronos", "analyst"}
        # Operator is NOT in the list → should later skip.
        assert info.explicit_to_me is False
        assert info.explicit_to_other is True


# ------------------------------------------------------------------------------
# Cross-agent addressing guard (the bug that started this refactor)
# ------------------------------------------------------------------------------


class TestCrossAgentGuard:
    @pytest.mark.asyncio
    async def test_operator_skips_when_analyst_is_addressed(self):
        """The exact production bug: @analystagnt arrives, Operator must skip."""
        router = _make_router("operator")
        event = _make_event(
            text="@analystagnt а ты что думаешь про product hunt?",
            sender_id=USER_ID,
        )
        decision = await router.decide(event, client=MagicMock())
        assert decision.should_respond is False
        assert "not me" in decision.reason
        assert decision.addressing is not None
        assert decision.addressing.target_agents == {"analyst"}

    @pytest.mark.asyncio
    async def test_analyst_responds_when_analyst_is_addressed(self):
        router = _make_router("analyst")
        event = _make_event(
            text="@analystagnt а ты что думаешь?", sender_id=USER_ID,
        )
        decision = await router.decide(event, client=MagicMock())
        assert decision.should_respond is True
        assert decision.tier == 1

    @pytest.mark.asyncio
    async def test_multi_mention_each_addressed_agent_answers(self):
        """`@kronos @analyst` → both Kronos and Analyst run Tier 1, others skip."""
        for agent_name, expected in [
            ("kronos", True), ("analyst", True),
            ("operator", False), ("reviewer", False), ("creative", False), ("strategist", False),
        ]:
            router = _make_router(agent_name)
            event = _make_event(
                text="@kronosagnt и @analystagnt что думаете?",
                sender_id=USER_ID,
            )
            decision = await router.decide(event, client=MagicMock())
            assert decision.should_respond is expected, (
                f"{agent_name}: expected respond={expected}, got {decision}"
            )


# ------------------------------------------------------------------------------
# Tier 2 relevance — fires only when nobody specific is addressed
# ------------------------------------------------------------------------------


class TestTier2Relevance:
    @pytest.mark.asyncio
    async def test_high_relevance_triggers_tier2(self):
        router = _make_router("operator")
        event = _make_event(text="надо быстрее запускаться", sender_id=USER_ID)
        with patch.object(router, "_check_relevance", new=AsyncMock(return_value=9)):
            decision = await router.decide(event, client=MagicMock())
        assert decision.should_respond is True
        assert decision.tier == 2

    @pytest.mark.asyncio
    async def test_low_relevance_skips(self):
        router = _make_router("operator")
        event = _make_event(text="что-то абстрактное", sender_id=USER_ID)
        with patch.object(router, "_check_relevance", new=AsyncMock(return_value=3)):
            decision = await router.decide(event, client=MagicMock())
        assert decision.should_respond is False

    @pytest.mark.asyncio
    async def test_tier2_skipped_when_other_agent_addressed(self):
        """Relevance check must NOT run if another agent is addressed."""
        router = _make_router("operator")
        event = _make_event(
            text="@analystagnt про product hunt", sender_id=USER_ID,
        )
        # If _check_relevance gets called at all, this mock will raise.
        with patch.object(router, "_check_relevance", new=AsyncMock(side_effect=AssertionError)):
            decision = await router.decide(event, client=MagicMock())
        assert decision.should_respond is False


# ------------------------------------------------------------------------------
# Tier 3 peer reaction — user-root required, cooldown, dedup
# ------------------------------------------------------------------------------


@pytest.mark.integration
class TestTier3PeerReaction:
    @pytest.mark.asyncio
    async def test_peer_reply_to_user_triggers_disagree_check(self):
        router = _make_router("kronos")
        # Peer reply to a user message. Body must NOT contain any known
        # agent name/alias — otherwise cross-agent guard fires first.
        user_root = MagicMock()
        user_root.sender_id = USER_ID
        event = _make_event(
            text="ответ на вопрос о лонче", sender_id=PEER_ANALYST_ID,
            reply_msg=user_root,
        )
        with patch.object(router, "_should_react_to_peer",
                          new=AsyncMock(return_value=True)):
            decision = await router.decide(event, client=MagicMock())
        assert decision.should_respond is True
        assert decision.tier == 3

    @pytest.mark.asyncio
    async def test_peer_reply_to_peer_is_skipped(self):
        """No peer→peer chains; bots must not debate each other forever."""
        router = _make_router("kronos")
        peer_parent = MagicMock()
        peer_parent.sender_id = PEER_REVIEWER_ID  # another peer, not user
        event = _make_event(
            text="добавляю свою точку зрения", sender_id=PEER_ANALYST_ID,
            reply_msg=peer_parent,
        )
        with patch.object(router, "_should_react_to_peer",
                          new=AsyncMock(side_effect=AssertionError)):
            decision = await router.decide(event, client=MagicMock())
        assert decision.should_respond is False
        assert "not replying to a user" in decision.reason

    @pytest.mark.asyncio
    async def test_standalone_peer_message_is_skipped(self):
        """Peer with no reply linkage has no user root → skip Tier 3."""
        router = _make_router("kronos")
        event = _make_event(text="спонтанное сообщение", sender_id=PEER_ANALYST_ID)
        decision = await router.decide(event, client=MagicMock())
        assert decision.should_respond is False

    @pytest.mark.asyncio
    async def test_peer_at_mentioning_me_bypasses_tier3_as_tier1(self):
        router = _make_router("kronos")
        event = _make_event(
            text="@kronosagnt что скажешь?", sender_id=PEER_ANALYST_ID,
        )
        decision = await router.decide(event, client=MagicMock())
        assert decision.should_respond is True
        assert decision.tier == 1

    @pytest.mark.asyncio
    async def test_peer_reaction_cooldown_blocks_repeat(self):
        """After one successful Tier 3 reaction, a second within cooldown is blocked.

        Cooldown fires before the msg-id dedup check; both guards land in
        the same "do not react again" behavior, so we just assert the
        second call is rejected — not the specific reason string.
        """
        router = _make_router("kronos")
        user_root = MagicMock()
        user_root.sender_id = USER_ID
        e1 = _make_event(text="вопрос A", sender_id=PEER_ANALYST_ID,
                         msg_id=500, reply_msg=user_root)
        e2 = _make_event(text="вопрос B", sender_id=PEER_ANALYST_ID,
                         msg_id=501, reply_msg=user_root)
        with patch.object(router, "_should_react_to_peer",
                          new=AsyncMock(return_value=True)):
            d1 = await router.decide(e1, client=MagicMock())
            assert d1.should_respond is True
            d2 = await router.decide(e2, client=MagicMock())
        assert d2.should_respond is False
        assert "cooldown" in d2.reason or "already reacted" in d2.reason


# ------------------------------------------------------------------------------
# should_still_respond — tier-aware post-delay recheck
# ------------------------------------------------------------------------------


class TestShouldStillRespond:
    @pytest.mark.asyncio
    async def test_tier1_always_still_respond(self):
        router = _make_router("kronos")
        event = _make_event(text="@kronosagnt", sender_id=USER_ID)
        # Even if iter_messages would report many peer replies, tier=1 wins.
        client = MagicMock()
        client.iter_messages = MagicMock(side_effect=AssertionError)
        assert await router.should_still_respond(event, client, tier=1) is True

    @pytest.mark.asyncio
    async def test_tier2_skips_after_peer_cap(self):
        router = _make_router("kronos")
        event = _make_event(text="q", sender_id=USER_ID, msg_id=777)

        # Build fake peer reply iterator that points at our event.
        class FakeMsg:
            def __init__(self, reply_to_id: int, sender_id: int):
                self.reply_to = MagicMock(reply_to_msg_id=reply_to_id)
                self.sender_id = sender_id

        peers = [FakeMsg(777, PEER_ANALYST_ID), FakeMsg(777, PEER_REVIEWER_ID)]

        class AsyncIter:
            def __init__(self, items):
                self._it = iter(items)
            def __aiter__(self): return self
            async def __anext__(self):
                try: return next(self._it)
                except StopIteration: raise StopAsyncIteration

        client = MagicMock()
        client.iter_messages = MagicMock(return_value=AsyncIter(peers))

        assert MAX_PEER_REPLIES == 2
        assert await router.should_still_respond(event, client, tier=2) is False

    @pytest.mark.asyncio
    async def test_tier3_also_subject_to_peer_cap(self):
        """Regression: should_still_respond used to ignore Tier 3."""
        router = _make_router("kronos")
        event = _make_event(text="q", sender_id=USER_ID, msg_id=888)

        class FakeMsg:
            def __init__(self, reply_to_id: int, sender_id: int):
                self.reply_to = MagicMock(reply_to_msg_id=reply_to_id)
                self.sender_id = sender_id

        peers = [FakeMsg(888, PEER_ANALYST_ID), FakeMsg(888, PEER_REVIEWER_ID)]

        class AsyncIter:
            def __init__(self, items):
                self._it = iter(items)
            def __aiter__(self): return self
            async def __anext__(self):
                try: return next(self._it)
                except StopIteration: raise StopAsyncIteration

        client = MagicMock()
        client.iter_messages = MagicMock(return_value=AsyncIter(peers))

        assert await router.should_still_respond(event, client, tier=3) is False
