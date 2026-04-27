"""Tests for kronos.swarm_store — shared cross-agent ledger.

Covers:
  * Record/read of swarm_messages (idempotent per PK).
  * Atomic claim arbitration — winner by (tier ASC, eta ASC, name ASC).
  * Tier 1 bypasses arbitration and the implicit-reply cap.
  * Cap enforcement for Tier 2/3 (default = 2 implicit replies per root).
  * Claim lifecycle: claim → cancel → can_send_claim loses.
  * Shared user facts: add (idempotent), FTS5 search, access bookkeeping.
  * Metrics increment/read.
  * Retention prune by age.

Every test uses a fresh temp file and resets the module-level singletons
that live in ``kronos.db`` and ``kronos.swarm_store`` so tests do not
leak SQLite handles across each other.
"""

from __future__ import annotations

import time

import pytest


@pytest.fixture
def swarm(tmp_path, monkeypatch):
    """Fresh SwarmStore backed by a temp file, isolated per test.

    ``kronos.config.settings`` is imported by other modules at import
    time, so re-instantiating the Settings object does NOT update their
    bindings. We mutate the existing singleton in place via setattr and
    then clear the SafeDB / SwarmStore caches so the next call opens the
    freshly-configured path.
    """
    swarm_path = tmp_path / "swarm.db"
    db_dir = tmp_path / "agent"
    db_dir.mkdir()

    from kronos.config import settings as _settings
    monkeypatch.setattr(_settings, "swarm_db_path", str(swarm_path))
    monkeypatch.setattr(_settings, "db_dir", str(db_dir))
    monkeypatch.setattr(_settings, "db_path", str(db_dir / "session.db"))

    # Reset SafeDB + SwarmStore singletons so they pick up the new paths.
    from kronos import db as _db
    _db._instances.clear()
    import kronos.swarm_store as ss
    ss._singleton = None

    from kronos.swarm_store import get_swarm
    return get_swarm()


class TestRecordMessages:
    def test_record_inbound_and_read_back(self, swarm):
        swarm.record_inbound_message(
            chat_id=10, topic_id=None, msg_id=1, reply_to_msg_id=None,
            sender_id=42, sender_type="user", agent_name=None, text="hi",
        )
        rows = swarm.get_recent_messages(chat_id=10, topic_id=None)
        assert len(rows) == 1
        assert rows[0]["text"] == "hi"
        assert rows[0]["sender_type"] == "user"

    def test_idempotent_on_primary_key(self, swarm):
        for _ in range(3):
            swarm.record_inbound_message(
                chat_id=10, topic_id=None, msg_id=1, reply_to_msg_id=None,
                sender_id=42, sender_type="user", agent_name=None, text="hi",
            )
        rows = swarm.get_recent_messages(chat_id=10, topic_id=None)
        assert len(rows) == 1

    def test_topic_isolation(self, swarm):
        swarm.record_inbound_message(
            chat_id=10, topic_id=5, msg_id=1, reply_to_msg_id=None,
            sender_id=42, sender_type="user", agent_name=None, text="in topic",
        )
        swarm.record_inbound_message(
            chat_id=10, topic_id=None, msg_id=1, reply_to_msg_id=None,
            sender_id=42, sender_type="user", agent_name=None, text="general",
        )
        assert len(swarm.get_recent_messages(chat_id=10, topic_id=5)) == 1
        assert len(swarm.get_recent_messages(chat_id=10, topic_id=None)) == 1

    def test_outbound_recorded_as_agent(self, swarm):
        swarm.record_outbound_message(
            chat_id=10, topic_id=None, msg_id=999, reply_to_msg_id=1,
            agent_name="kronos", text="my reply",
        )
        rows = swarm.get_recent_messages(chat_id=10, topic_id=None)
        assert rows[0]["sender_type"] == "agent"
        assert rows[0]["agent_name"] == "kronos"


class TestArbitration:
    def _claim(self, swarm, agent: str, tier: int, eta_offset: float, *, msg_id: int = 1):
        swarm.claim_reply(
            chat_id=100, topic_id=None, root_msg_id=1, trigger_msg_id=msg_id,
            agent_name=agent, tier=tier, eta_ts=time.time() + eta_offset,
        )

    def _can_send(self, swarm, agent: str, tier: int):
        return swarm.can_send_claim(
            chat_id=100, topic_id=None, root_msg_id=1,
            agent_name=agent, tier=tier,
        )

    def test_earliest_eta_wins_at_same_tier(self, swarm):
        self._claim(swarm, "kronos", tier=2, eta_offset=1.0)
        self._claim(swarm, "analyst", tier=2, eta_offset=2.0)
        assert self._can_send(swarm, "kronos", tier=2).won is True
        assert self._can_send(swarm, "analyst", tier=2).won is False

    def test_lower_tier_beats_earlier_eta(self, swarm):
        """Tier ordering dominates eta_ts — Tier 2 beats Tier 3 even if T3 eta is earlier."""
        self._claim(swarm, "kronos", tier=3, eta_offset=0.1)
        self._claim(swarm, "analyst", tier=2, eta_offset=5.0)
        assert self._can_send(swarm, "analyst", tier=2).won is True
        assert self._can_send(swarm, "kronos", tier=3).won is False

    def test_tier1_bypasses_arbitration(self, swarm):
        """Explicit @mention always sends, even against earlier Tier 2 claim."""
        self._claim(swarm, "analyst", tier=2, eta_offset=0.1)
        self._claim(swarm, "operator", tier=1, eta_offset=5.0)
        out = self._can_send(swarm, "operator", tier=1)
        assert out.won is True
        assert "tier-1" in out.reason.lower()

    def test_cancel_releases_slot(self, swarm):
        self._claim(swarm, "kronos", tier=2, eta_offset=1.0, msg_id=1)
        self._claim(swarm, "analyst", tier=2, eta_offset=2.0, msg_id=2)
        swarm.cancel_claim(
            chat_id=100, topic_id=None, trigger_msg_id=1,
            agent_name="kronos", reason="test",
        )
        # Now analyst is the last remaining active claim → wins.
        assert self._can_send(swarm, "analyst", tier=2).won is True

    def test_cap_enforced_for_implicit_replies(self, swarm):
        """Default cap = 2 implicit replies per root, across agents and tiers > 1."""
        for i, agent in enumerate(["kronos", "analyst"], start=1):
            self._claim(swarm, agent, tier=2, eta_offset=i * 0.1, msg_id=i)
            swarm.mark_sent(
                chat_id=100, topic_id=None, trigger_msg_id=i,
                agent_name=agent, reply_msg_id=i * 100,
            )
        self._claim(swarm, "reviewer", tier=2, eta_offset=0.3, msg_id=3)
        out = self._can_send(swarm, "reviewer", tier=2)
        assert out.won is False
        assert "cap" in out.reason.lower()

    def test_tier1_ignores_cap(self, swarm):
        """Explicit addressing wins even after the implicit cap is full."""
        for i, agent in enumerate(["kronos", "analyst"], start=1):
            self._claim(swarm, agent, tier=2, eta_offset=i * 0.1, msg_id=i)
            swarm.mark_sent(
                chat_id=100, topic_id=None, trigger_msg_id=i,
                agent_name=agent, reply_msg_id=i * 100,
            )
        self._claim(swarm, "reviewer", tier=1, eta_offset=0.3, msg_id=3)
        assert self._can_send(swarm, "reviewer", tier=1).won is True


class TestSharedUserFacts:
    def test_add_and_search(self, swarm):
        swarm.add_shared_fact(
            user_id="u1", fact="User runs a startup",
            source_agent="kronos",
        )
        found = swarm.search_shared_facts(user_id="u1", query="startup")
        assert any("startup" in f for f in found)

    def test_add_is_idempotent(self, swarm):
        first = swarm.add_shared_fact(user_id="u1", fact="same fact", source_agent="k")
        second = swarm.add_shared_fact(user_id="u1", fact="same fact", source_agent="k")
        assert first is True
        assert second is False  # duplicate — UNIQUE constraint

    def test_search_scoped_by_user(self, swarm):
        swarm.add_shared_fact(user_id="u1", fact="u1 fact", source_agent="k")
        swarm.add_shared_fact(user_id="u2", fact="u2 fact", source_agent="k")
        found_u1 = swarm.search_shared_facts(user_id="u1", query="fact")
        assert "u1 fact" in found_u1
        assert "u2 fact" not in found_u1

    def test_empty_query_returns_empty(self, swarm):
        swarm.add_shared_fact(user_id="u1", fact="anything", source_agent="k")
        assert swarm.search_shared_facts(user_id="u1", query="") == []
        assert swarm.search_shared_facts(user_id="u1", query="   ") == []

    def test_all_shared_facts(self, swarm):
        swarm.add_shared_fact(user_id="u1", fact="fact A", source_agent="k")
        swarm.add_shared_fact(user_id="u1", fact="fact B", source_agent="n")
        all_facts = swarm.all_shared_facts(user_id="u1")
        assert set(all_facts) == {"fact A", "fact B"}


class TestMetrics:
    def test_increment_and_read(self, swarm):
        swarm.incr_metric("addressing_respected", 3)
        swarm.incr_metric("addressing_respected", 2)
        swarm.incr_metric("duplicate_replies_avoided")
        metrics = swarm.get_metrics()
        assert metrics["addressing_respected"] == 5
        assert metrics["duplicate_replies_avoided"] == 1


class TestRetention:
    def test_prune_old_messages(self, swarm):
        # Manually age a message by patching created_at via direct SQL.
        swarm.record_inbound_message(
            chat_id=1, topic_id=None, msg_id=1, reply_to_msg_id=None,
            sender_id=42, sender_type="user", agent_name=None, text="old",
        )
        swarm.record_inbound_message(
            chat_id=1, topic_id=None, msg_id=2, reply_to_msg_id=None,
            sender_id=42, sender_type="user", agent_name=None, text="new",
        )
        ancient = time.time() - 100 * 86400  # 100 days ago
        swarm._db.write(
            "UPDATE swarm_messages SET created_at = ? WHERE msg_id = 1",
            (ancient,),
        )
        deleted = swarm.prune_old_messages(older_than_days=90)
        assert deleted == 1
        remaining = swarm.get_recent_messages(chat_id=1, topic_id=None)
        assert len(remaining) == 1
        assert remaining[0]["text"] == "new"
