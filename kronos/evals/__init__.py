"""Agent CI: golden scenarios captured from production, replayed deterministically.

Three pieces:

* ``scenario`` — the file format plus ``ScriptedChatModel``, which replays the
  model turns a real run produced;
* ``capture`` — builds scenarios out of the durable turn journal;
* ``runner`` — replays a suite and checks expectations (structural, budget,
  content), with no keys and no network.

The split matters: cassettes (``kronos.cassettes``) give "same input, same code,
same answer" replay, while scenarios survive a **changed prompt** — which is the
change most worth diffing.
"""

from kronos.evals.capture import CaptureReport, capture_thread, capture_turn, list_turns
from kronos.evals.diff import DiffError, DiffReport, diff_reports, run_suite_at_ref
from kronos.evals.runner import ScenarioResult, SuiteResult, run_scenario, run_suite
from kronos.evals.scenario import (
    SCENARIO_FILE,
    Expectations,
    Scenario,
    ScenarioError,
    ScriptedChatModel,
    discover,
)

__all__ = [
    "SCENARIO_FILE",
    "CaptureReport",
    "DiffError",
    "DiffReport",
    "Expectations",
    "Scenario",
    "ScenarioError",
    "ScenarioResult",
    "ScriptedChatModel",
    "SuiteResult",
    "capture_thread",
    "capture_turn",
    "diff_reports",
    "discover",
    "list_turns",
    "run_scenario",
    "run_suite",
    "run_suite_at_ref",
]
