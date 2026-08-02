"""CLI surface for policy report and audit verify (moat phase 9.6)."""

import json

import pytest
import yaml

from kronos import audit
from kronos.cli import build_parser, main
from kronos.config import settings
from kronos.policy import ENV_POLICY_FILE, reset_policy


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    monkeypatch.delenv(ENV_POLICY_FILE, raising=False)
    db_dir = tmp_path / "data" / "gov"
    db_dir.mkdir(parents=True)
    monkeypatch.setattr(settings, "db_dir", str(db_dir))
    monkeypatch.setattr(settings, "db_path", str(db_dir / "session.db"))
    audit.reset_chain_cache()
    audit._audit_dir = None
    reset_policy()
    yield db_dir / "logs"
    audit.reset_chain_cache()
    audit._audit_dir = None
    reset_policy()


def test_parser_exposes_governance_commands():
    parser = build_parser()

    assert parser.parse_args(["policy", "report"]).policy_command == "report"
    assert parser.parse_args(["policy", "report", "--json"]).as_json is True
    assert parser.parse_args(["audit", "verify"]).audit_command == "verify"


def test_policy_report_without_a_file_says_defaults(capsys):
    code = main(["policy", "report"])
    out = capsys.readouterr().out

    assert code == 0
    assert "no policy file" in out
    assert "default" in out
    assert "Copy policy.example.yaml" in out


def test_policy_report_shows_the_winning_source(tmp_path, monkeypatch, capsys):
    path = tmp_path / "policy.yaml"
    path.write_text(
        yaml.safe_dump({"capabilities": {"server_ops": False, "dynamic_tools": True}}, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setenv(ENV_POLICY_FILE, str(path))
    monkeypatch.setattr(settings, "enable_server_ops", True)  # explicit env override
    reset_policy()

    code = main(["policy", "report"])
    out = capsys.readouterr().out

    assert code == 0
    assert "enable_server_ops" in out
    # The env override wins and is labelled as such.
    server_ops_line = next(line for line in out.splitlines() if line.startswith("enable_server_ops"))
    assert "True" in server_ops_line and "env" in server_ops_line
    dynamic_line = next(line for line in out.splitlines() if line.startswith("enable_dynamic_tools"))
    assert "True" in dynamic_line and "policy" in dynamic_line


def test_policy_report_json_is_machine_readable(tmp_path, monkeypatch, capsys):
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump({"budgets": {"daily_usd": 3.0}}, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv(ENV_POLICY_FILE, str(path))
    reset_policy()

    assert main(["policy", "report", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["source_path"] == str(path)
    assert payload["policy"]["budgets"]["daily_usd"] == 3.0
    assert any(row["setting"] == "tool_approvals_enabled" for row in payload["settings"])


def test_policy_report_fails_on_an_invalid_policy(tmp_path, monkeypatch, capsys):
    path = tmp_path / "policy.yaml"
    path.write_text("egress:\n  mode: nonsense\n", encoding="utf-8")
    monkeypatch.setenv(ENV_POLICY_FILE, str(path))
    reset_policy()

    code = main(["policy", "report"])

    assert code == 1
    assert "Policy error:" in capsys.readouterr().out


def test_audit_verify_passes_on_a_clean_chain(clean, capsys):
    audit.log_tool_event("tool_call", {"name": "get_status", "args": {}})

    code = main(["audit", "verify"])
    out = capsys.readouterr().out

    assert code == 0
    assert "[OK]" in out
    assert "1 entries verified" in out


def test_audit_verify_detects_tampering(clean, capsys):
    audit.log_tool_event("tool_call", {"name": "get_status", "args": {}})
    audit.log_tool_event("tool_call", {"name": "deploy_service", "args": {}})

    path = clean / "tool_calls.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    del rows[0]  # cover up the first call
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    code = main(["audit", "verify"])
    out = capsys.readouterr().out

    assert code == 1
    assert "Chain broken in: tool_calls.jsonl" in out


def test_audit_verify_json_output(clean, capsys):
    audit.log_tool_event("tool_call", {"name": "get_status", "args": {}})

    assert main(["audit", "verify", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert {row["log"] for row in payload} == {"tool_calls.jsonl", "audit.jsonl", "credentials.jsonl"}
    assert all(row["ok"] for row in payload)


def test_doctor_reports_governance_state(clean, capsys):
    from kronos.cli import run_doctor

    run_doctor()
    out = capsys.readouterr().out

    assert "Governance policy" in out
    assert "Egress" in out
    assert "Audit chain" in out
