"""Scenario runner: structural, budget and content checks (moat phase 8.4)."""

import pytest
import yaml

from kronos.evals.runner import STATUS_ERROR, STATUS_FAIL, STATUS_PASS, run_scenario, run_suite
from kronos.evals.scenario import Scenario


def _write(tmp_path, name: str, payload: dict) -> Scenario:
    directory = tmp_path / "suite" / name
    directory.mkdir(parents=True)
    (directory / "scenario.yaml").write_text(
        yaml.safe_dump({"name": name, **payload}, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return Scenario.load(directory)


def _expenses_payload(**expect) -> dict:
    return {
        "input": "покажи расходы за день",
        "script": [
            {"tool_calls": [{"name": "get_expenses", "args": {"scope": "day"}}]},
            {"content": "За день 12.5 USD: кофе."},
        ],
        "tool_outputs": {"get_expenses": ["12.5 USD за кофе"]},
        "expect": {"tools_called": ["get_expenses"], **expect},
    }


@pytest.mark.asyncio
async def test_passing_scenario_reports_metrics(tmp_path):
    scenario = _write(tmp_path, "expenses", _expenses_payload())

    result = await run_scenario(scenario)

    assert result.status == STATUS_PASS
    assert result.tools_called == ["get_expenses"]
    assert result.tool_calls == 1
    assert result.model_turns == 2
    assert "12.5" in result.answer


@pytest.mark.asyncio
async def test_stub_tool_returns_recorded_output(tmp_path):
    """The model's answer is scripted, but the tool output must reach the loop."""
    scenario = _write(
        tmp_path,
        "echo",
        {
            "input": "проверь",
            "script": [
                {"tool_calls": [{"name": "get_status", "args": {}}]},
                {"content": "готово"},
            ],
            "tool_outputs": {"get_status": ["всё зелено"]},
            "expect": {},
        },
    )

    result = await run_scenario(scenario)

    assert result.status == STATUS_PASS
    assert result.tools_called == ["get_status"]


@pytest.mark.asyncio
async def test_missing_expected_tool_fails(tmp_path):
    payload = _expenses_payload()
    payload["expect"]["tools_called"] = ["get_expenses", "query_notion"]
    scenario = _write(tmp_path, "missing-tool", payload)

    result = await run_scenario(scenario)

    assert result.status == STATUS_FAIL
    assert any(check.name == "tools_called" and "query_notion" in check.detail for check in result.failures)


@pytest.mark.asyncio
async def test_forbidden_tool_fails(tmp_path):
    payload = _expenses_payload(tools_forbidden=["get_expenses"])
    scenario = _write(tmp_path, "forbidden", payload)

    result = await run_scenario(scenario)

    assert result.status == STATUS_FAIL
    assert [check.name for check in result.failures] == ["tools_forbidden"]


@pytest.mark.asyncio
async def test_tool_order_is_checked_when_requested(tmp_path):
    payload = {
        "input": "сначала одно, потом другое",
        "script": [
            {"tool_calls": [{"name": "get_b", "args": {}}]},
            {"tool_calls": [{"name": "get_a", "args": {}}]},
            {"content": "готово"},
        ],
        "tool_outputs": {"get_a": ["a"], "get_b": ["b"]},
        "expect": {"tools_called": ["get_a", "get_b"], "ordered": True},
    }
    scenario = _write(tmp_path, "ordered", payload)

    result = await run_scenario(scenario)

    assert result.status == STATUS_FAIL
    assert any(check.name == "tools_order" for check in result.failures)


@pytest.mark.asyncio
async def test_budget_ceiling_fails_when_exceeded(tmp_path):
    payload = {
        "input": "много вызовов",
        "script": [
            {"tool_calls": [{"name": "get_a", "args": {}}]},
            {"tool_calls": [{"name": "get_a", "args": {}}]},
            {"tool_calls": [{"name": "get_a", "args": {}}]},
            {"content": "готово"},
        ],
        "tool_outputs": {"get_a": ["a"]},
        "expect": {"max_tool_calls": 2},
    }
    scenario = _write(tmp_path, "budget", payload)

    result = await run_scenario(scenario)

    assert result.status == STATUS_FAIL
    assert any(check.name == "max_tool_calls" and "3 > 2" in check.detail for check in result.failures)


@pytest.mark.asyncio
async def test_approval_gating_is_observed_from_real_policy(tmp_path, monkeypatch):
    """add_expense is on the default approval list — the scenario should see that."""
    from kronos.config import settings

    monkeypatch.setattr(settings, "tool_approvals_enabled", True)
    payload = {
        "input": "добавь расход",
        "script": [
            {"tool_calls": [{"name": "add_expense", "args": {"amount": 12.5}}]},
            {"content": "добавил"},
        ],
        "tool_outputs": {"add_expense": ["ok"]},
        "expect": {"approval_required_for": ["add_expense"]},
    }
    scenario = _write(tmp_path, "approval", payload)

    result = await run_scenario(scenario)

    assert result.status == STATUS_PASS
    assert result.approvals_requested == ["add_expense"]


@pytest.mark.asyncio
async def test_approval_expectation_fails_when_policy_stops_gating(tmp_path, monkeypatch):
    from kronos.config import settings

    monkeypatch.setattr(settings, "tool_approvals_enabled", False)
    payload = {
        "input": "добавь расход",
        "script": [
            {"tool_calls": [{"name": "add_expense", "args": {"amount": 12.5}}]},
            {"content": "добавил"},
        ],
        "tool_outputs": {"add_expense": ["ok"]},
        "expect": {"approval_required_for": ["add_expense"]},
    }
    scenario = _write(tmp_path, "approval-off", payload)

    result = await run_scenario(scenario)

    assert result.status == STATUS_FAIL
    assert any(check.name == "approval_required_for" for check in result.failures)


@pytest.mark.asyncio
async def test_content_assertions(tmp_path):
    payload = _expenses_payload(must_mention=["12.5"], must_not_mention=["ошибка"])
    scenario = _write(tmp_path, "content-ok", payload)
    assert (await run_scenario(scenario)).status == STATUS_PASS

    bad = _expenses_payload(must_mention=["итого за неделю"])
    scenario = _write(tmp_path, "content-bad", bad)
    result = await run_scenario(scenario)
    assert result.status == STATUS_FAIL
    assert any(check.name == "must_mention" for check in result.failures)


@pytest.mark.asyncio
async def test_script_exhaustion_is_reported_as_error(tmp_path):
    """The agent wanting one more model turn than recorded is a behaviour change."""
    payload = {
        "input": "незаконченный сценарий",
        "script": [{"tool_calls": [{"name": "get_a", "args": {}}]}],
        "tool_outputs": {"get_a": ["a"]},
        "expect": {},
    }
    scenario = _write(tmp_path, "short-script", payload)

    result = await run_scenario(scenario)

    assert result.status == STATUS_ERROR
    assert "script has 1" in result.error


@pytest.mark.asyncio
async def test_run_suite_aggregates_and_renders(tmp_path):
    _write(tmp_path, "good", _expenses_payload())
    _write(tmp_path, "bad", _expenses_payload(tools_forbidden=["get_expenses"]))

    suite = await run_suite(tmp_path / "suite")
    rendered = suite.render()

    assert suite.passed == 1 and suite.failed == 1
    assert suite.ok is False
    assert "[PASS] good" in rendered and "[FAIL] bad" in rendered
    payload = suite.to_dict()
    assert payload["passed"] == 1
    assert {scenario["name"] for scenario in payload["scenarios"]} == {"good", "bad"}


@pytest.mark.asyncio
async def test_suite_runs_without_network_or_keys(tmp_path, monkeypatch):
    """A hermetic run: any socket use would be a bug in the runner."""
    import socket

    def no_network(*args, **kwargs):
        raise AssertionError("scenario runs must not open sockets")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _write(tmp_path, "hermetic", _expenses_payload())

    suite = await run_suite(tmp_path / "suite")

    assert suite.ok is True
