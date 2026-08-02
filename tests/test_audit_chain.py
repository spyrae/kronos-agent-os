"""Tamper-evident audit logs (moat phase 9.5)."""

import json

import pytest

from kronos import audit
from kronos.config import settings


@pytest.fixture
def audit_dir(tmp_path, monkeypatch):
    db_dir = tmp_path / "data" / "chain"
    db_dir.mkdir(parents=True)
    monkeypatch.setattr(settings, "db_path", str(db_dir / "session.db"))
    monkeypatch.setattr(settings, "db_dir", str(db_dir))
    audit.reset_chain_cache()
    audit._audit_dir = None
    yield db_dir / "logs"
    audit.reset_chain_cache()
    audit._audit_dir = None


def _log_calls(count: int) -> None:
    for index in range(count):
        audit.log_tool_event("tool_call", {"name": f"get_status_{index}", "args": {"n": index}})


def _lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_first_entry_starts_from_genesis(audit_dir):
    _log_calls(1)

    rows = _lines(audit_dir / "tool_calls.jsonl")

    assert rows[0]["prev_hash"] == audit.GENESIS_HASH
    assert len(rows[0]["entry_hash"]) == 64


def test_entries_link_to_each_other(audit_dir):
    _log_calls(3)

    rows = _lines(audit_dir / "tool_calls.jsonl")

    assert rows[1]["prev_hash"] == rows[0]["entry_hash"]
    assert rows[2]["prev_hash"] == rows[1]["entry_hash"]


def test_untouched_chain_verifies(audit_dir):
    _log_calls(4)

    ok, detail = audit.verify_chain(audit_dir / "tool_calls.jsonl")

    assert ok is True
    assert "4 entries verified" in detail


def test_modified_content_is_detected(audit_dir):
    _log_calls(3)
    path = audit_dir / "tool_calls.jsonl"
    rows = _lines(path)
    rows[1]["tool"] = "something_else"  # edit without recomputing the hash
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")

    ok, detail = audit.verify_chain(path)

    assert ok is False
    assert "tool_calls.jsonl:2: content was modified" in detail


def test_removed_entry_is_detected(audit_dir):
    """The classic cover-up: delete the line that recorded the bad call."""
    _log_calls(3)
    path = audit_dir / "tool_calls.jsonl"
    rows = _lines(path)
    del rows[1]
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")

    ok, detail = audit.verify_chain(path)

    assert ok is False
    assert "broken link" in detail


def test_reordered_entries_are_detected(audit_dir):
    _log_calls(3)
    path = audit_dir / "tool_calls.jsonl"
    rows = _lines(path)
    rows[0], rows[1] = rows[1], rows[0]
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")

    ok, detail = audit.verify_chain(path)

    assert ok is False
    assert "broken link" in detail


def test_appended_forgery_without_the_tip_is_detected(audit_dir):
    """An attacker who appends a line must know the current tip to stay valid."""
    _log_calls(2)
    path = audit_dir / "tool_calls.jsonl"
    forged = {"ts": "2026-07-25T00:00:00+0000", "event": "tool_call", "tool": "deploy_service"}
    forged["prev_hash"] = "0" * 64
    forged["entry_hash"] = audit.entry_hash(forged)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(forged) + "\n")

    ok, detail = audit.verify_chain(path)

    assert ok is False
    assert "tool_calls.jsonl:3: broken link" in detail


def test_pre_chain_entries_are_skipped_not_flagged(audit_dir):
    """Upgrading an existing install must not report a broken chain."""
    path = audit_dir / "tool_calls.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps({"ts": "old", "tool": f"get_status_{index}"}) for index in range(3)) + "\n",
        encoding="utf-8",
    )
    audit.reset_chain_cache()

    _log_calls(2)  # new entries continue from here
    ok, detail = audit.verify_chain(path)

    assert ok is True
    assert "2 entries verified" in detail
    assert "3 pre-chain entries skipped" in detail


def test_unchained_entry_inserted_after_chaining_fails(audit_dir):
    """A line someone slipped in later has no hash and must not pass."""
    _log_calls(2)
    path = audit_dir / "tool_calls.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": "sneaky", "tool": "deploy_service"}) + "\n")

    ok, detail = audit.verify_chain(path)

    assert ok is False
    assert "unchained entry inserted after chaining began" in detail


def test_chain_continues_across_process_restarts(audit_dir):
    """A fresh process must pick up the tip from the file, not restart at genesis."""
    _log_calls(2)
    audit.reset_chain_cache()  # simulates a restart
    _log_calls(1)

    ok, _ = audit.verify_chain(audit_dir / "tool_calls.jsonl")
    rows = _lines(audit_dir / "tool_calls.jsonl")

    assert ok is True
    assert rows[2]["prev_hash"] == rows[1]["entry_hash"]


def test_request_log_is_chained_too(audit_dir):
    audit.log_request(
        user_id="u1",
        session_id="s1",
        tier="lite",
        input_text="привет",
        output_text="здравствуй",
        duration_ms=12,
    )

    ok, detail = audit.verify_chain(audit_dir / "audit.jsonl")

    assert ok is True
    assert "1 entries verified" in detail


def test_cost_log_is_not_chained(audit_dir):
    """cost.jsonl is an aggregation input; chaining it would add nothing."""
    audit.log_request(user_id="u1", session_id="s1", tier="lite", input_text="a", output_text="b", duration_ms=1)

    rows = _lines(audit_dir / "cost.jsonl")

    assert rows and "entry_hash" not in rows[0]
    assert audit.get_daily_cost()["requests"] == 1  # aggregation still works


def test_verify_audit_logs_covers_every_chained_file(audit_dir):
    _log_calls(1)
    audit.log_request(user_id="u1", session_id="s1", tier="lite", input_text="a", output_text="b", duration_ms=1)

    results = audit.verify_audit_logs(audit_dir)

    # Named rather than compared to CHAINED_LOGS: a log dropped from the tuple
    # is exactly the regression this should catch, not silently agree with.
    assert {name for name, _, _ in results} == {"tool_calls.jsonl", "audit.jsonl", "credentials.jsonl"}
    assert all(ok for _, ok, _ in results)


def test_missing_log_is_not_a_failure(audit_dir):
    ok, detail = audit.verify_chain(audit_dir / "tool_calls.jsonl")

    assert ok is True
    assert "no log yet" in detail


@pytest.mark.asyncio
async def test_concurrent_writers_produce_a_valid_chain(audit_dir):
    """Several async tasks log at once; two entries sharing prev_hash would
    look like tampering, so the tip update is locked."""
    import asyncio

    async def writer(index: int):
        await asyncio.sleep(0)
        audit.log_tool_event("tool_call", {"name": f"tool_{index}", "args": {}})

    await asyncio.gather(*(writer(index) for index in range(20)))

    ok, detail = audit.verify_chain(audit_dir / "tool_calls.jsonl")

    assert ok is True, detail
    assert "20 entries verified" in detail
