"""Behaviour diff between two suite runs (moat phase 8.5)."""

from kronos.evals.diff import (
    KIND_ANSWER_CHANGED,
    KIND_APPROVALS_CHANGED,
    KIND_FIXED,
    KIND_NEW_FAILURE,
    KIND_REMOVED,
    KIND_TOOLS_CHANGED,
    KIND_TURNS_CHANGED,
    diff_reports,
)


def _scenario(
    name="expenses",
    status="pass",
    tools=("get_expenses",),
    approvals=(),
    model_turns=2,
    tool_calls=1,
    answer_chars=40,
    checks=None,
    error="",
):
    return {
        "name": name,
        "status": status,
        "error": error,
        "metrics": {
            "tools_called": list(tools),
            "approvals_requested": list(approvals),
            "model_turns": model_turns,
            "tool_calls": tool_calls,
            "answer_chars": answer_chars,
        },
        "checks": checks or [{"name": "tools_called", "passed": status == "pass", "detail": ""}],
    }


def _report(*scenarios):
    return {"suite": "golden", "scenarios": list(scenarios)}


def _kinds(report, scenario="expenses"):
    return {entry.kind for entry in report.entries if entry.scenario == scenario}


def test_identical_runs_produce_no_diff():
    report = diff_reports(_report(_scenario()), _report(_scenario()))

    assert report.empty is True
    assert "No behaviour change" in report.render_markdown()


def test_new_failure_is_flagged_with_the_failing_check():
    head = _scenario(
        status="fail",
        checks=[{"name": "approval_required_for", "passed": False, "detail": "ran without approval: add_expense"}],
    )

    report = diff_reports(_report(_scenario()), _report(head))

    assert KIND_NEW_FAILURE in _kinds(report)
    assert len(report.regressions) == 1
    assert "approval_required_for" in report.regressions[0].detail
    assert "⚠️ 1 new failure" in report.render_markdown()


def test_fixed_scenario_is_reported():
    report = diff_reports(_report(_scenario(status="fail")), _report(_scenario()))

    assert KIND_FIXED in _kinds(report)
    assert report.regressions == []


def test_tool_path_change_is_detected():
    head = _scenario(tools=("get_expenses", "query_notion"), tool_calls=2)

    report = diff_reports(_report(_scenario()), _report(head))
    kinds = _kinds(report)

    assert KIND_TOOLS_CHANGED in kinds
    assert KIND_TURNS_CHANGED in kinds


def test_approval_change_is_its_own_signal():
    """Policy edits show up here even when every check still passes."""
    head = _scenario(approvals=("add_expense",))

    report = diff_reports(_report(_scenario()), _report(head))

    assert KIND_APPROVALS_CHANGED in _kinds(report)
    assert report.regressions == []


def test_small_answer_wobble_is_ignored_but_large_change_is_not():
    """LLM prose is not a spec, so only a material size change is reported."""
    small = diff_reports(_report(_scenario(answer_chars=100)), _report(_scenario(answer_chars=105)))
    assert small.empty is True

    large = diff_reports(_report(_scenario(answer_chars=100)), _report(_scenario(answer_chars=400)))
    assert KIND_ANSWER_CHANGED in _kinds(large)


def test_added_and_removed_scenarios_are_listed():
    base = _report(_scenario(name="kept"), _scenario(name="gone"))
    head = _report(_scenario(name="kept"), _scenario(name="fresh"))

    report = diff_reports(base, head)

    assert {entry.kind for entry in report.entries if entry.scenario == "gone"} == {KIND_REMOVED}
    assert {entry.kind for entry in report.entries if entry.scenario == "fresh"} == {"scenario_added"}


def test_failure_counts_are_carried_into_the_report():
    base = _report(_scenario(name="a"), _scenario(name="b", status="fail"))
    head = _report(_scenario(name="a", status="fail"), _scenario(name="b", status="fail"))

    report = diff_reports(base, head, base_ref="main", head_ref="HEAD")
    markdown = report.render_markdown()

    assert report.base_failed == 1 and report.head_failed == 2
    assert "Failing scenarios: 1 → 2" in markdown
    assert "`main` → `HEAD`" in markdown


def test_error_status_detail_prefers_the_error_text():
    head = _scenario(status="error", error="script exhausted", checks=[])

    report = diff_reports(_report(_scenario()), _report(head))

    assert "script exhausted" in report.regressions[0].detail
