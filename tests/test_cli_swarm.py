"""CLI surface for the swarm report and the offline swarm demo (moat 11.4/11.5)."""

import json
import time

import pytest

from kronos.cli import build_parser, main
from kronos.config import settings


@pytest.fixture
def swarm_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "agent_name", "kronos")
    import kronos.db as _db
    import kronos.swarm_store as swarm_store

    # `main()` reads the ledger through the process-wide singleton, so a stale one
    # would show the previous test's rows in this test's "empty" report.
    _db._instances.clear()
    monkeypatch.setattr(swarm_store, "_singleton", None)

    store = swarm_store.get_swarm()
    yield store
    _db._instances.clear()


def _reply(store, agent: str, tier: int, msg: int) -> None:
    store.claim_reply(
        chat_id=-1,
        topic_id=0,
        root_msg_id=msg,
        trigger_msg_id=msg,
        agent_name=agent,
        tier=tier,
        eta_ts=time.time(),
    )
    store.mark_sent(chat_id=-1, topic_id=0, trigger_msg_id=msg, agent_name=agent, reply_msg_id=msg + 1)


# --- parser -------------------------------------------------------------------


def test_parser_exposes_the_swarm_report():
    parser = build_parser()

    assert parser.parse_args(["swarm", "report"]).swarm_command == "report"
    assert parser.parse_args(["swarm", "report"]).period == "week"
    assert parser.parse_args(["swarm", "report", "--day"]).period == "day"
    assert parser.parse_args(["swarm", "report", "--period", "month"]).period == "month"
    assert parser.parse_args(["swarm", "report", "--json"]).as_json is True


def test_parser_exposes_the_swarm_demo():
    assert build_parser().parse_args(["demo", "--swarm"]).swarm is True


# --- report -------------------------------------------------------------------


def test_report_renders_markdown(swarm_db, capsys):
    _reply(swarm_db, "kronos", 1, 10)

    code = main(["swarm", "report", "--week"])
    out = capsys.readouterr().out

    assert code == 0
    assert "Отчёт роя" in out
    assert "| kronos |" in out


def test_report_json_is_machine_readable(swarm_db, capsys):
    _reply(swarm_db, "nexus", 2, 11)

    code = main(["swarm", "report", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["period"] == "week"
    assert payload["agents"][0]["agent"] == "nexus"


def test_report_on_an_empty_ledger_still_exits_clean(swarm_db, capsys):
    assert main(["swarm", "report", "--day"]) == 0
    assert "Ответов за период не было." in capsys.readouterr().out


def test_an_unknown_period_fails_with_a_message(swarm_db, capsys, monkeypatch):
    """argparse guards the CLI; the runner guards a bad programmatic call."""
    from kronos.cli import run_swarm_report

    code = run_swarm_report("fortnight", False)

    assert code == 1
    assert "unknown period" in capsys.readouterr().out


# --- demo ---------------------------------------------------------------------


def test_the_swarm_demo_runs_offline(capsys, monkeypatch):
    """No Telegram, no keys, no network — and it must show real coordination."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    code = main(["demo", "--swarm"])
    out = capsys.readouterr().out

    assert code == 0
    assert "owner of 'planning'" in out
    assert "escalated to strategist" in out
    assert "escalations_triggered: 1" in out
    assert "Отчёт роя" in out


def test_the_demo_leaves_the_live_registry_alone(capsys):
    from kronos.group_router import AGENT_PROFILES

    before = {name: dict(prof) for name, prof in AGENT_PROFILES.items()}

    main(["demo", "--swarm"])

    assert AGENT_PROFILES == before, "the demo swaps the registry and must put it back"
