"""Replay a suite of golden scenarios and check expectations.

The run is hermetic by construction: a scripted model, stub tools built from the
scenario's recorded outputs, no network, no keys, no databases. What it exercises
is everything between the model and the user — tool wiring, call order, approval
gating, loop detection, output compaction, untrusted framing — which is exactly
the part that regresses silently.

Checks are properties, not prose snapshots: which tools ran, in what order, how
many times, whether an approval-gated tool was gated, and whether required
substrings survived to the answer.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool

from kronos.engine import react_loop, tool_requires_approval
from kronos.evals.scenario import Scenario, ScenarioError, ScriptedChatModel, discover

log = logging.getLogger("kronos.evals.runner")

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_ERROR = "error"


@dataclass
class CheckResult:
    """One expectation and how it went."""

    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class ScenarioResult:
    """Everything observed while replaying one scenario."""

    name: str
    status: str
    checks: list[CheckResult] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)
    approvals_requested: list[str] = field(default_factory=list)
    model_turns: int = 0
    tool_calls: int = 0
    answer: str = ""
    error: str = ""
    draft: bool = False

    @property
    def failures(self) -> list[CheckResult]:
        return [check for check in self.checks if not check.passed]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "draft": self.draft,
            "metrics": {
                "model_turns": self.model_turns,
                "tool_calls": self.tool_calls,
                "tools_called": self.tools_called,
                "approvals_requested": self.approvals_requested,
                "answer_chars": len(self.answer),
            },
            "checks": [check.to_dict() for check in self.checks],
            "error": self.error,
        }


@dataclass
class SuiteResult:
    """Aggregate outcome of a suite run."""

    suite: str
    results: list[ScenarioResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for result in self.results if result.status == STATUS_PASS)

    @property
    def failed(self) -> int:
        return sum(1 for result in self.results if result.status != STATUS_PASS)

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def to_dict(self) -> dict:
        return {
            "suite": self.suite,
            "passed": self.passed,
            "failed": self.failed,
            "scenarios": [result.to_dict() for result in self.results],
        }

    def render(self) -> str:
        lines = [f"Suite: {self.suite}", ""]
        for result in self.results:
            mark = {"pass": "PASS", "fail": "FAIL", "error": "ERR "}[result.status]
            draft = " (draft)" if result.draft else ""
            lines.append(f"[{mark}] {result.name}{draft}")
            lines.append(
                f"       model turns: {result.model_turns}, tool calls: {result.tool_calls}, "
                f"tools: {', '.join(result.tools_called) or 'none'}"
            )
            if result.error:
                lines.append(f"       error: {result.error}")
            for check in result.failures:
                lines.append(f"       ✗ {check.name}: {check.detail}")
        lines.append("")
        lines.append(f"{self.passed} passed, {self.failed} failed")
        return "\n".join(lines)


class _StubTool(BaseTool):
    """Tool that returns the outputs recorded for it, in order."""

    name: str = "stub"
    description: str = "recorded tool"
    outputs: list[str] = []
    position: int = 0

    def _run(self, *args: Any, **kwargs: Any) -> str:
        if not self.outputs:
            return "OK"
        index = min(self.position, len(self.outputs) - 1)
        self.position += 1
        return self.outputs[index]


def _build_tools(scenario: Scenario) -> list[BaseTool]:
    """One stub per tool the script calls, replying with recorded output."""
    tools: list[BaseTool] = []
    for name in dict.fromkeys(scenario.tool_names):
        tool = _StubTool(
            name=name,
            description=f"recorded tool '{name}' from scenario {scenario.name}",
            outputs=list(scenario.tool_outputs.get(name, [])),
        )
        tools.append(tool)
    return tools


def _check_expectations(
    scenario: Scenario, result: ScenarioResult, *, scenario_script_length: int
) -> list[CheckResult]:
    expect = scenario.expect
    checks: list[CheckResult] = []
    called = result.tools_called

    if expect.tools_called:
        missing = [name for name in expect.tools_called if name not in called]
        checks.append(
            CheckResult(
                "tools_called",
                not missing,
                f"missing: {', '.join(missing)}" if missing else "",
            )
        )
        if expect.ordered:
            sequence = [name for name in called if name in expect.tools_called]
            ordered_ok = sequence[: len(expect.tools_called)] == expect.tools_called
            checks.append(
                CheckResult(
                    "tools_order",
                    ordered_ok,
                    f"expected {expect.tools_called}, saw {sequence}" if not ordered_ok else "",
                )
            )

    if expect.tools_forbidden:
        present = [name for name in expect.tools_forbidden if name in called]
        checks.append(CheckResult("tools_forbidden", not present, f"called: {', '.join(present)}" if present else ""))

    if expect.approval_required_for:
        ungated = [name for name in expect.approval_required_for if name not in result.approvals_requested]
        checks.append(
            CheckResult(
                "approval_required_for",
                not ungated,
                f"ran without approval: {', '.join(ungated)}" if ungated else "",
            )
        )

    if expect.max_tool_calls:
        within = result.tool_calls <= expect.max_tool_calls
        checks.append(
            CheckResult(
                "max_tool_calls",
                within,
                f"{result.tool_calls} > {expect.max_tool_calls}" if not within else "",
            )
        )

    # Always checked: a run that used fewer model turns than were recorded took
    # a different path, even if every other expectation still holds.
    checks.append(
        CheckResult(
            "script_consumed",
            result.model_turns == scenario_script_length,
            f"used {result.model_turns} of {scenario_script_length} recorded model turns"
            if result.model_turns != scenario_script_length
            else "",
        )
    )

    answer = result.answer.lower()
    if expect.must_mention:
        absent = [phrase for phrase in expect.must_mention if phrase.lower() not in answer]
        checks.append(CheckResult("must_mention", not absent, f"absent: {', '.join(absent)}" if absent else ""))
    if expect.must_not_mention:
        present = [phrase for phrase in expect.must_not_mention if phrase.lower() in answer]
        checks.append(CheckResult("must_not_mention", not present, f"present: {', '.join(present)}" if present else ""))

    return checks


async def run_scenario(scenario: Scenario, *, max_turns: int = 12) -> ScenarioResult:
    """Replay one scenario against a scripted model and stub tools."""
    result = ScenarioResult(name=scenario.name, status=STATUS_PASS, draft=scenario.draft)
    model = ScriptedChatModel(scenario.script, scenario_name=scenario.name)
    tools = _build_tools(scenario)

    def on_tool_event(event: str, payload: dict[str, Any]) -> None:
        if event == "tool_call":
            result.tools_called.append(str(payload.get("name") or ""))
            result.tool_calls += 1

    def needs_approval(tool: BaseTool, args: dict) -> bool:
        """Ask the real policy, then let the call proceed.

        The scenario needs to know *whether* a call would be gated, not to sit
        waiting for a human — so the answer is recorded and execution continues.
        """
        if tool_requires_approval(tool, args):
            result.approvals_requested.append(tool.name)
        return False

    try:
        outcome = await react_loop(
            model,
            [HumanMessage(content=scenario.input)],
            tools,
            max_turns=max_turns,
            on_tool_event=on_tool_event,
            needs_tool_approval=needs_approval,
        )
        result.answer = outcome.content
        result.model_turns = model.calls
        if model.exhausted:
            # The agent asked for more model turns than the recorded run used.
            # react_loop absorbed the failure, so surface it here — this is a
            # behaviour change, not a passing scenario.
            result.status = STATUS_ERROR
            result.error = f"the agent requested model turn {model.calls + 1} but the script has {model.script_length}"
            return result
    except ScenarioError as e:
        result.status = STATUS_ERROR
        result.error = str(e)
        result.model_turns = model.calls
        return result
    except Exception as e:  # a crash in the agent is a scenario failure, not a suite crash
        result.status = STATUS_ERROR
        result.error = f"{type(e).__name__}: {e}"
        result.model_turns = model.calls
        log.warning("Scenario '%s' raised: %s", scenario.name, e)
        return result

    result.checks = _check_expectations(scenario, result, scenario_script_length=model.script_length)
    if result.failures:
        result.status = STATUS_FAIL
    return result


async def run_suite(suite_dir: str | Path, *, max_turns: int = 12) -> SuiteResult:
    """Replay every scenario in a suite directory."""
    root = Path(suite_dir)
    suite = SuiteResult(suite=root.name or str(root))
    for scenario in discover(root):
        suite.results.append(await run_scenario(scenario, max_turns=max_turns))
    return suite
