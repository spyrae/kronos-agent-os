"""Tests for kronos.security.cost_tracking and the guardian/swarm wiring.

Closes the review's "cost guardian is dead" finding:
  * ``record_cost`` now has a caller (the always-on cost callback), so the
    per-conversation session limit actually bites;
  * the daily budget is swarm-wide (summed over all six agents' ledger rows),
    not a per-process total each agent can blow independently;
  * Codex calls (no provider-reported usage) fall back to a length estimate.

The ``test_callback_records_through_model_ainvoke`` case is the important one:
it drives a real ``model.ainvoke`` with the handler attached inside a set audit
context and asserts the cost landed — proving the per-request ``session_id``
actually reaches ``on_llm_end`` (the wiring the whole fix depends on).
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult


@pytest.fixture
def cost_env(tmp_path, monkeypatch):
    """Isolated swarm ledger + fresh guardian/handler singletons per test."""
    swarm_path = tmp_path / "swarm.db"
    db_dir = tmp_path / "agent"
    db_dir.mkdir()

    from kronos.config import settings
    monkeypatch.setattr(settings, "swarm_db_path", str(swarm_path))
    monkeypatch.setattr(settings, "db_dir", str(db_dir))
    monkeypatch.setattr(settings, "db_path", str(db_dir / "session.db"))
    monkeypatch.setattr(settings, "agent_name", "nexus")

    import kronos.security.cost_guardian as cg
    import kronos.security.cost_tracking as ct
    import kronos.swarm_store as ss
    from kronos import db as _db

    _db._instances.clear()
    ss._singleton = None
    cg._guardian = None
    ct._handler = None
    yield
    _db._instances.clear()
    ss._singleton = None
    cg._guardian = None
    ct._handler = None


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------

def test_pricing_known_default_codex_and_env(monkeypatch):
    from kronos.security.cost_tracking import _price_for, estimate_cost_usd

    assert _price_for("deepseek-chat") == (0.27, 1.10)
    assert _price_for("gpt-5.5") == (0.0, 0.0)  # Codex OAuth = zero marginal
    assert _price_for("totally-unknown-model") == (0.50, 1.50)  # default

    # 1M input + 1M output at the DeepSeek rate.
    assert estimate_cost_usd("deepseek-chat", 1_000_000, 1_000_000) == pytest.approx(1.37)
    # A zero-priced model never accrues, whatever the token count.
    assert estimate_cost_usd("gpt-5.5", 5_000_000, 5_000_000) == 0.0

    monkeypatch.setenv("KAOS_MODEL_PRICE_DEEPSEEK_CHAT_INPUT", "1.00")
    assert _price_for("deepseek-chat")[0] == 1.00


# --------------------------------------------------------------------------
# Usage extraction
# --------------------------------------------------------------------------

def test_extract_usage_prefers_usage_metadata():
    from kronos.security.cost_tracking import _extract_usage

    message = AIMessage(
        content="hi",
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )
    result = LLMResult(
        generations=[[ChatGeneration(message=message)]],
        llm_output={"model_name": "deepseek-chat"},
    )
    assert _extract_usage(result) == ("deepseek-chat", 10, 5)


def test_extract_usage_falls_back_to_llm_output_token_usage():
    from kronos.security.cost_tracking import _extract_usage

    message = AIMessage(content="hi")
    result = LLMResult(
        generations=[[ChatGeneration(message=message)]],
        llm_output={
            "model_name": "gpt-4.1-mini",
            "token_usage": {"prompt_tokens": 20, "completion_tokens": 8},
        },
    )
    assert _extract_usage(result) == ("gpt-4.1-mini", 20, 8)


def test_handler_estimates_when_provider_reports_no_usage(cost_env):
    """Codex CLI returns only text — estimate from stashed input + output len."""
    from kronos.security.cost_tracking import get_cost_callbacks
    from kronos.swarm_store import get_swarm

    handler = get_cost_callbacks()[0]
    run_id = "run-codex-1"
    handler.on_chat_model_start(
        {}, [[HumanMessage(content="x" * 350)]], run_id=run_id,
    )
    message = AIMessage(content="y" * 70, response_metadata={"model_name": "deepseek-chat"})
    result = LLMResult(generations=[[ChatGeneration(message=message)]])
    handler.on_llm_end(result, run_id=run_id)

    daily = get_swarm().daily_cost()
    # 350 chars / 3.5 = 100 input tokens, 70 / 3.5 = 20 output tokens.
    assert daily["input_tokens"] == 100
    assert daily["output_tokens"] == 20
    assert daily["cost_usd"] > 0
    # pending map is cleaned up after the call.
    assert run_id not in handler._pending_input_chars


# --------------------------------------------------------------------------
# Recording wiring (swarm ledger + session guardian)
# --------------------------------------------------------------------------

def test_record_llm_cost_updates_swarm_and_session(cost_env):
    from kronos.audit import reset_tool_audit_context, set_tool_audit_context
    from kronos.security.cost_guardian import get_guardian
    from kronos.security.cost_tracking import record_llm_cost
    from kronos.swarm_store import get_swarm

    token = set_tool_audit_context(agent="nexus", session_id="12345", thread_id="12345", user_id="u")
    try:
        record_llm_cost("deepseek-chat", 1_000_000, 0, 0.27)
    finally:
        reset_tool_audit_context(token)

    assert get_swarm().daily_cost()["cost_usd"] == pytest.approx(0.27)
    assert get_guardian()._session_costs["12345"] == pytest.approx(0.27)


def test_record_llm_cost_without_session_still_logs_daily(cost_env):
    """No session in context (e.g. cron/digest) → daily ledger still updated."""
    from kronos.security.cost_guardian import get_guardian
    from kronos.security.cost_tracking import record_llm_cost
    from kronos.swarm_store import get_swarm

    record_llm_cost("deepseek-chat", 0, 1_000_000, 1.10)  # no audit context set

    assert get_swarm().daily_cost()["cost_usd"] == pytest.approx(1.10)
    assert get_guardian()._session_costs == {}


# --------------------------------------------------------------------------
# Guardian limits
# --------------------------------------------------------------------------

def test_session_limit_enforced_after_record(cost_env):
    from kronos.security.cost_guardian import get_guardian

    guardian = get_guardian()
    allowed, _ = guardian.check_budget(session_id="s1")
    assert allowed

    guardian.record_cost("s1", 1.5)  # over the $1 session limit
    allowed, reason = guardian.check_budget(session_id="s1")
    assert not allowed
    assert "Session cost limit" in reason


def test_daily_limit_is_swarm_wide(cost_env):
    from kronos.security.cost_guardian import get_guardian
    from kronos.swarm_store import get_swarm

    swarm = get_swarm()
    # Two different agents each add cost; the daily cap sees the sum.
    swarm.add_cost(agent="nexus", cost_usd=3.0)
    swarm.add_cost(agent="lacuna", cost_usd=3.0)

    allowed, reason = get_guardian().check_budget(session_id="s1")
    assert not allowed  # 6.0 >= 5.0 daily limit
    assert "Daily cost limit" in reason


def test_add_cost_upserts_and_separates_days(cost_env):
    from kronos.swarm_store import get_swarm

    swarm = get_swarm()
    swarm.add_cost(agent="nexus", cost_usd=0.1, input_tokens=10, output_tokens=5)
    swarm.add_cost(agent="nexus", cost_usd=0.2, input_tokens=20, output_tokens=5)

    today = swarm.daily_cost()
    assert today["cost_usd"] == pytest.approx(0.3)
    assert today["requests"] == 2
    assert today["input_tokens"] == 30

    swarm.add_cost(agent="nexus", cost_usd=9.9, day="2000-01-01")
    assert swarm.daily_cost()["cost_usd"] == pytest.approx(0.3)  # today untouched
    assert swarm.daily_cost(day="2000-01-01")["cost_usd"] == pytest.approx(9.9)
    assert swarm.per_agent_daily_cost()["nexus"] == pytest.approx(0.3)


# --------------------------------------------------------------------------
# End-to-end: the ContextVar must reach on_llm_end through model.ainvoke
# --------------------------------------------------------------------------

async def test_callback_records_through_model_ainvoke(cost_env):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

    from kronos.audit import reset_tool_audit_context, set_tool_audit_context
    from kronos.security.cost_guardian import get_guardian
    from kronos.security.cost_tracking import get_cost_callbacks
    from kronos.swarm_store import get_swarm

    reply = AIMessage(
        content="pong",
        usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        response_metadata={"model_name": "deepseek-chat"},
    )
    model = FakeMessagesListChatModel(responses=[reply])
    handler = get_cost_callbacks()[0]

    token = set_tool_audit_context(agent="nexus", session_id="chat-1", thread_id="chat-1", user_id="u")
    try:
        await model.ainvoke([HumanMessage(content="ping")], config={"callbacks": [handler]})
    finally:
        reset_tool_audit_context(token)

    # Cost reached BOTH ledgers → session_id propagated into on_llm_end.
    assert get_swarm().daily_cost()["cost_usd"] > 0
    assert get_guardian()._session_costs.get("chat-1", 0) > 0
