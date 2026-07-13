"""Cost /stats aggregation + budget degradation (roadmap 6.2)."""

import json
import time

import pytest

from kronos.config import settings


@pytest.fixture
def cost_env(tmp_path, monkeypatch):
    db_dir = tmp_path / "kronos"
    (db_dir / "logs").mkdir(parents=True)
    monkeypatch.setattr(settings, "db_path", str(db_dir / "session.db"))
    monkeypatch.setattr(settings, "db_dir", str(db_dir))
    monkeypatch.setattr(settings, "agent_name", "kronos")

    import kronos.audit as audit

    audit._audit_dir = None  # force re-resolution to the tmp path
    return db_dir


def _write_costs(db_dir, entries):
    with open(db_dir / "logs" / "cost.jsonl", "a") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def test_cost_report_aggregates_by_tier(cost_env):
    from kronos.security.cost_stats import cost_report

    _write_costs(cost_env, [
        {"ts": _now(), "tier": "standard", "cost_usd": 0.02, "input_tokens": 100, "output_tokens": 50},
        {"ts": _now(), "tier": "lite", "cost_usd": 0.005, "input_tokens": 20, "output_tokens": 10},
        {"ts": _now(), "tier": "lite", "cost_usd": 0.005, "input_tokens": 20, "output_tokens": 10},
    ])
    report = cost_report("today")
    assert report["total"]["requests"] == 3
    assert abs(report["total"]["cost"] - 0.03) < 1e-6
    assert report["by_tier"]["lite"]["requests"] == 2
    assert report["by_tier"]["standard"]["requests"] == 1


def test_cost_report_today_excludes_old_entries(cost_env):
    from kronos.security.cost_stats import cost_report

    _write_costs(cost_env, [
        {"ts": "2020-01-01T00:00:00", "tier": "standard", "cost_usd": 1.0, "input_tokens": 1, "output_tokens": 1},
        {"ts": _now(), "tier": "lite", "cost_usd": 0.01, "input_tokens": 1, "output_tokens": 1},
    ])
    report = cost_report("today")
    assert report["total"]["requests"] == 1  # 2020 entry excluded


def test_swarm_cost_by_agent(cost_env):
    from kronos.security.cost_stats import swarm_cost_by_agent

    _write_costs(cost_env, [
        {"ts": _now(), "tier": "lite", "cost_usd": 0.01, "input_tokens": 1, "output_tokens": 1},
    ])
    nexus = cost_env.parent / "nexus"
    (nexus / "logs").mkdir(parents=True)
    with open(nexus / "logs" / "cost.jsonl", "w") as handle:
        handle.write(json.dumps(
            {"ts": _now(), "tier": "standard", "cost_usd": 0.05, "input_tokens": 1, "output_tokens": 1}
        ) + "\n")

    breakdown = swarm_cost_by_agent("today")
    assert breakdown["kronos"] == 0.01
    assert breakdown["nexus"] == 0.05


def test_should_degrade_crosses_threshold(monkeypatch):
    from kronos.security import cost_guardian

    guardian = cost_guardian.CostGuardian(daily_limit=5.0)
    # The daily total now comes from the shared swarm ledger, not the per-agent
    # audit file — patch that source.
    monkeypatch.setattr(cost_guardian, "_swarm_daily_cost", lambda: {"cost_usd": 4.5})  # 90%
    assert guardian.should_degrade() is True
    monkeypatch.setattr(cost_guardian, "_swarm_daily_cost", lambda: {"cost_usd": 1.0})  # 20%
    assert guardian.should_degrade() is False


async def test_stats_command_formats_report(cost_env):
    from kronos.bridge import _handle_stats_command

    _write_costs(cost_env, [
        {"ts": _now(), "tier": "lite", "cost_usd": 0.01, "input_tokens": 5, "output_tokens": 5},
    ])
    result = await _handle_stats_command("/stats")
    assert result is not None
    assert "Расходы" in result
    assert "lite" in result
    assert "Дневной бюджет" in result


async def test_stats_command_ignores_non_stats_text(cost_env):
    from kronos.bridge import _handle_stats_command

    assert await _handle_stats_command("привет") is None
