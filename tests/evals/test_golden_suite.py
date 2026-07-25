"""Deploy-gate: the bundled golden suite must pass (moat phase 8.6).

This is the pytest face of `kaos eval run` so the suite gates a deploy through
the same `-m eval` marker as the swarm invariants — deterministic, offline, no
provider keys.
"""

from pathlib import Path

import pytest

from kronos.evals.runner import run_suite

pytestmark = pytest.mark.eval

SUITE_DIR = Path(__file__).resolve().parents[1] / "evals" / "suites" / "golden"


@pytest.mark.asyncio
async def test_golden_suite_passes():
    result = await run_suite(SUITE_DIR)

    assert result.results, f"no scenarios found in {SUITE_DIR}"
    assert result.ok, result.render()


@pytest.mark.asyncio
async def test_golden_suite_covers_the_policy_surface():
    """The suite is only a gate if it asserts the things that can regress."""
    from kronos.evals.scenario import discover

    scenarios = discover(SUITE_DIR)
    checked = {
        "approval": any(scenario.expect.approval_required_for for scenario in scenarios),
        "forbidden": any(scenario.expect.tools_forbidden for scenario in scenarios),
        "budget": any(scenario.expect.max_tool_calls for scenario in scenarios),
        "content": any(scenario.expect.must_mention for scenario in scenarios),
        "no_tool_answer": any(scenario.expect.max_tool_calls == 0 for scenario in scenarios),
    }

    assert all(checked.values()), f"golden suite is missing coverage: {checked}"
    assert not any(scenario.draft for scenario in scenarios), "draft scenarios must not gate deploys"
