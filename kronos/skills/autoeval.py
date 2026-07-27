"""Run a skill's own scenario before trusting it (moat 12.3).

A signature proves who wrote a skill, not that it works. Since Phase 8 a scenario
is a single self-contained YAML — the model's turns are scripted inside it — so a
publisher can ship one next to `SKILL.md` and an installer can replay it offline,
with no provider key and no network.

What that check is and is not: it replays the publisher's own recorded turn and
asserts the publisher's own expectations. It catches a skill that contradicts
itself (forbidden tool called, budget blown, claim missing) and a skill broken in
transit. It cannot tell you the skill is a good idea. That is why a passing
scenario permits activation but never demands it, and why a skill without one is
marked `unverified` rather than refused — most skills will have none for a while,
and refusing them would leave the registry empty and the marking meaningless.

The eval verdict is written into the skill's frontmatter. That is safe by
construction: the checksum covers an allowlist of semantic fields, and these are
not in it, so recording a local verdict cannot invalidate a published checksum.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from kronos.skills.store import SkillStore

log = logging.getLogger("kronos.skills.autoeval")

# Where a skill keeps its scenario, relative to the skill directory.
SCENARIO_RELPATH = "evals/scenario.yaml"

EVAL_PASSED = "passed"
EVAL_FAILED = "failed"
EVAL_MISSING = "none"
EVAL_ERROR = "error"


@dataclass
class EvalOutcome:
    """What the scenario said, in a form the installer can act on."""

    status: str = EVAL_MISSING
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.status == EVAL_PASSED

    @property
    def ran(self) -> bool:
        return self.status in (EVAL_PASSED, EVAL_FAILED)


def scenario_url(skill_url: str, explicit: str = "") -> str:
    """Where the scenario lives for a skill fetched from `skill_url`.

    Convention over configuration: a sibling `evals/scenario.yaml` next to
    SKILL.md, so publishing a check needs no extra index field. An explicit URL in
    the index wins when the layout differs.
    """
    if explicit:
        return explicit
    if not skill_url:
        return ""
    if skill_url.startswith("github:"):
        base = skill_url.removesuffix("/SKILL.md").rstrip("/")
        return f"{base}/{SCENARIO_RELPATH}"
    if skill_url.endswith("/SKILL.md"):
        return skill_url[: -len("SKILL.md")] + SCENARIO_RELPATH
    return ""


def fetch_scenario(url: str, skill_dir: Path, *, declared: bool = False) -> Path | None:
    """Fetch a scenario next to an installed skill. None when there is none.

    Goes through the same egress-checked fetcher as the skill itself; a 404 is the
    normal case, not an error.

    `declared` decides what a *non-scenario* response means. Plenty of hosts answer
    200 with an HTML error page for a missing file, and the conventional path is a
    guess — so garbage from a guessed URL means "no scenario here". Garbage from a
    URL the publisher put in the index is their broken check, and is kept so the
    eval reports it.
    """
    if not url:
        return None

    from kronos.skills.hub import _fetch_url, _resolve_source

    resolved = _resolve_source(url) if url.startswith("github:") else url
    if not resolved:
        return None
    try:
        payload = _fetch_url(resolved)
    except Exception as e:
        log.info("No scenario fetched from %s: %s", resolved, e)
        return None

    if not declared and not _looks_like_scenario(payload):
        log.info("Response from %s is not a scenario; treating the skill as having none", resolved)
        return None

    target = skill_dir / SCENARIO_RELPATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload, encoding="utf-8")
    return target


def _looks_like_scenario(payload: str) -> bool:
    """Cheap shape check: valid YAML mapping carrying a script."""
    import yaml

    try:
        parsed = yaml.safe_load(payload)
    except yaml.YAMLError:
        return False
    return isinstance(parsed, dict) and bool(parsed.get("script"))


async def run_skill_eval(skill_dir: Path) -> EvalOutcome:
    """Replay the skill's scenario. Hermetic: no keys, no network, no cassettes."""
    scenario_path = Path(skill_dir) / SCENARIO_RELPATH
    if not scenario_path.is_file():
        return EvalOutcome(EVAL_MISSING, "skill ships no scenario")

    from kronos.evals.runner import run_scenario
    from kronos.evals.scenario import Scenario, ScenarioError

    try:
        scenario = Scenario.load(scenario_path)
    except ScenarioError as e:
        return EvalOutcome(EVAL_ERROR, f"scenario is unusable: {e}")

    try:
        result = await run_scenario(scenario)
    except Exception as e:  # pragma: no cover - defensive
        return EvalOutcome(EVAL_ERROR, f"scenario could not be replayed: {e}")

    from kronos.evals.runner import STATUS_ERROR, STATUS_PASS

    if result.status == STATUS_PASS:
        return EvalOutcome(EVAL_PASSED, f"scenario '{scenario.name}' passed")

    failures = [check.detail or check.name for check in result.failures] or [result.error or "unknown failure"]
    detail = f"scenario '{scenario.name}' failed: {'; '.join(failures[:3])}"
    # A scenario that could not run at all is a broken check, not a verdict on the
    # skill — the installer treats the two the same way but the report should not.
    return EvalOutcome(EVAL_ERROR if result.status == STATUS_ERROR else EVAL_FAILED, detail)


def record_outcome(store: SkillStore, name: str, outcome: EvalOutcome) -> None:
    """Persist the verdict on the skill, so `verify` and the UI can show it."""
    store.set_meta(
        name,
        {
            "eval_status": outcome.status,
            "eval_detail": outcome.detail,
        },
    )


def name_conflicts(name: str, store: SkillStore, tool_names: list[str] | None = None) -> str:
    """Why this name cannot be installed, or "" when it is free.

    A skill that shadows a tool name is worse than a duplicate: `load_skill` and
    the tool router would disagree about what the model just asked for.
    """
    if store.get(name) is not None:
        return f"a skill named '{name}' is already installed"
    for tool_name in tool_names or known_tool_names():
        if tool_name == name:
            return f"'{name}' is the name of an existing tool"
    return ""


def known_tool_names() -> list[str]:
    """Tool names available without building an agent.

    Best-effort on purpose: the full tool set depends on configured MCP servers
    and capability gates, so this covers the built-ins a skill could shadow and
    stays quiet when a module is unavailable.
    """
    names: list[str] = []
    for module_name, attribute in (
        ("kronos.tools.reminders", None),
        ("kronos.tools.handoff", None),
        ("kronos.tools.council", None),
        ("kronos.tools.memory_ask", None),
        ("kronos.tools.skills_tools", None),
    ):
        try:
            module = __import__(module_name, fromlist=["*"])
        except Exception as e:  # pragma: no cover - optional extras
            log.debug("Could not inspect %s for tool names: %s", module_name, e)
            continue
        for value in vars(module).values():
            tool_name = getattr(value, "name", None)
            if isinstance(tool_name, str) and tool_name and callable(getattr(value, "invoke", None)):
                names.append(tool_name)
        if attribute:  # pragma: no cover - reserved for future explicit lists
            names.extend(getattr(module, attribute, []))
    return sorted(set(names))
