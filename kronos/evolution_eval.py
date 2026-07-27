"""Measure a persona proposal before a human is asked about it (moat 12.4).

Self-improvement used to arrive as an assertion: "this edit will improve the
reaction". Nothing checked it, so every proposal cost the owner a read.

What can honestly be measured here is narrower than the roadmap assumed, and the
difference matters enough to state plainly:

* Golden scenarios replay a **scripted** model (Phase 8): the model's turns come
  from the scenario file, so they do not change when the system prompt changes. A
  persona edit therefore cannot move a scripted scenario's *answer*. Claiming a
  quality delta from this suite would be a lie dressed as a number.
* What the suite genuinely proves is that the patched persona still runs: the
  prompt assembles, the engine's invariants (tool gating, approvals, budgets) hold,
  and no scenario starts failing. A persona long enough to blow a turn budget, or
  malformed enough to break prompt assembly, is caught here.
* The rest of the signal is structural and cheap: does the prompt still build, how
  much longer does it get, and is the proposal already present in the file (LLMs
  re-propose the same guidance for weeks).

So a proposal carries a measurement that can *refute* it and cannot flatter it.
`measured_quality: false` is part of the report, not a footnote.

The patched persona is applied to a copy of the workspace, never to the live one —
persona files are not in git, so the worktree trick from Phase 8.5 does not apply
here.
"""

import logging
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger("kronos.evolution_eval")

# A persona section this much larger than the file it joins is suspicious on its
# own: prompts are the most expensive real estate in the system.
PROMPT_GROWTH_WARN_PCT = 25.0

# Near-duplicate detection: proportion of the proposal's lines already present.
DUPLICATE_LINE_THRESHOLD = 0.6


def _target_file(workspace, target: str) -> Path:
    return {"soul": workspace.soul, "identity": workspace.identity}[target]


def patched_workspace(proposal: dict, *, into: Path) -> Path:
    """Copy the live workspace and apply the proposal to the copy."""
    from kronos.workspace import Workspace, ws

    source = ws.root
    destination = into / source.name
    shutil.copytree(source, destination, dirs_exist_ok=True)

    patched = Workspace(destination)
    target_file = _target_file(patched, proposal["target"])
    target_file.parent.mkdir(parents=True, exist_ok=True)
    with open(target_file, "a", encoding="utf-8") as handle:
        handle.write(f"\n\n## Evolution (proposed)\n_{proposal.get('rationale', '')}_\n\n{proposal['proposal']}\n")
    return destination


def prompt_delta(proposal: dict, patched_root: Path) -> dict:
    """Does the patched persona still assemble, and how much bigger is it?"""
    import kronos.workspace as workspace_module
    from kronos.persona import build_system_prompt
    from kronos.workspace import Workspace

    original = workspace_module.ws
    try:
        before = build_system_prompt()
    except Exception as e:
        return {"built": False, "error": f"the current persona does not build: {e}"}

    workspace_module.ws = Workspace(patched_root)
    try:
        after = build_system_prompt()
    except Exception as e:
        return {"built": False, "error": f"patched persona does not build: {e}"}
    finally:
        workspace_module.ws = original

    growth = ((len(after) - len(before)) / len(before) * 100) if before else 0.0
    return {
        "built": True,
        "chars_before": len(before),
        "chars_after": len(after),
        "growth_pct": round(growth, 2),
    }


def duplicate_ratio(proposal: dict) -> float:
    """How much of the proposal is already in the target file.

    Persona generation runs weekly off the same feedback, so re-proposing existing
    guidance is the common failure, not a rare one.
    """
    from kronos.workspace import ws

    target_file = _target_file(ws, proposal["target"])
    if not target_file.is_file():
        return 0.0
    existing = {line.strip().lower() for line in target_file.read_text(encoding="utf-8").splitlines() if line.strip()}
    lines = [line.strip().lower() for line in str(proposal["proposal"]).splitlines() if line.strip()]
    if not lines:
        return 0.0
    return round(sum(1 for line in lines if line in existing) / len(lines), 2)


async def measure_proposal(proposal: dict, *, suite_dir: Path | None = None) -> dict:
    """Everything measurable about one proposal, as a report the DB can hold."""
    from kronos.cli import DEFAULT_EVAL_SUITE
    from kronos.evals.diff import diff_reports
    from kronos.evals.runner import run_suite

    report: dict = {
        "measured_quality": False,
        "why_not": (
            "golden scenarios replay a scripted model, so a persona edit cannot change "
            "their answers; this measures that the patched persona still runs"
        ),
        "duplicate_ratio": 0.0,
        "prompt": {},
        "suite": {},
        "entries": [],
        "regressions": [],
        "notes": [],
    }

    try:
        report["duplicate_ratio"] = duplicate_ratio(proposal)
    except Exception as e:  # pragma: no cover - defensive
        report["notes"].append(f"duplicate check failed: {e}")

    suite = Path(suite_dir) if suite_dir else Path(__file__).resolve().parent.parent / DEFAULT_EVAL_SUITE
    if not suite.is_dir():
        report["notes"].append(f"no scenario suite at {suite}; proposal is unmeasured")
        return report

    with tempfile.TemporaryDirectory(prefix="kaos-persona-eval-") as tmp:
        try:
            patched_root = patched_workspace(proposal, into=Path(tmp))
        except Exception as e:
            report["notes"].append(f"could not build a patched workspace: {e}")
            return report

        report["prompt"] = prompt_delta(proposal, patched_root)
        if not report["prompt"].get("built", False):
            report["regressions"].append(
                {
                    "scenario": "system-prompt",
                    "kind": "prompt_broken",
                    "detail": report["prompt"].get("error", "prompt did not build"),
                }
            )
            return report

        growth = report["prompt"].get("growth_pct", 0.0)
        if growth > PROMPT_GROWTH_WARN_PCT:
            report["notes"].append(f"the prompt grows {growth:.1f}% — every turn pays for that")

        base = await run_suite(suite)
        head = await _run_suite_with_workspace(suite, patched_root)

    diff = diff_reports(base.to_dict(), head.to_dict(), base_ref="current", head_ref="proposed")
    report["suite"] = {
        "base_failed": diff.base_failed,
        "head_failed": diff.head_failed,
        "scenarios": len(base.results),
    }
    report["entries"] = [entry.to_dict() for entry in diff.entries]
    report["regressions"] = [entry.to_dict() for entry in diff.regressions]
    if diff.empty:
        report["notes"].append("no behaviour change in the offline suite")
    return report


async def _run_suite_with_workspace(suite: Path, patched_root: Path):
    """Run the suite with the patched workspace swapped in, then swap back."""
    import kronos.workspace as workspace_module
    from kronos.evals.runner import run_suite
    from kronos.workspace import Workspace

    original = workspace_module.ws
    workspace_module.ws = Workspace(patched_root)
    try:
        return await run_suite(suite)
    finally:
        workspace_module.ws = original


def regression_pct(report: dict) -> float:
    """Share of scenarios that started failing, as a percentage."""
    total = int(report.get("suite", {}).get("scenarios", 0) or 0)
    regressions = len(report.get("regressions", []) or [])
    if not total:
        return 100.0 if regressions else 0.0
    return round(regressions / total * 100, 2)


def verdict(report: dict, *, max_regression_pct: float) -> tuple[bool, str]:
    """Should a human be asked about this proposal at all?

    Auto-rejection is for proposals that are measurably worse or plainly
    redundant. Everything else reaches the owner — the point is to spend their
    attention on judgement calls, not to hide changes from them.
    """
    broken = [entry for entry in report.get("regressions", []) if entry.get("kind") == "prompt_broken"]
    if broken:
        return False, broken[0].get("detail", "the patched persona does not build")

    pct = regression_pct(report)
    if pct > max_regression_pct:
        names = ", ".join(entry.get("scenario", "?") for entry in report.get("regressions", [])[:3])
        return False, f"{pct:.0f}% of scenarios regressed ({names})"

    if report.get("duplicate_ratio", 0.0) >= DUPLICATE_LINE_THRESHOLD:
        return False, f"{report['duplicate_ratio'] * 100:.0f}% of the proposal is already in the file"

    return True, "no measurable regression"


def render_report(report: dict) -> str:
    """Short human summary — what goes into Telegram and `/persona show`."""
    lines: list[str] = []

    prompt = report.get("prompt") or {}
    if prompt.get("built"):
        lines.append(f"Промпт: +{prompt.get('growth_pct', 0):.1f}% ({prompt.get('chars_after', 0)} симв.)")
    elif prompt:
        lines.append(f"Промпт НЕ собирается: {prompt.get('error', 'unknown')}")

    suite = report.get("suite") or {}
    if suite:
        lines.append(
            f"Сценарии: {suite.get('scenarios', 0)}, падений {suite.get('base_failed', 0)} → {suite.get('head_failed', 0)}"
        )

    entries = report.get("entries") or []
    if entries:
        for entry in entries[:5]:
            lines.append(f"• {entry.get('scenario')}: {entry.get('kind')} — {entry.get('detail')}")
    elif suite:
        lines.append("• поведение офлайн-сьюта не изменилось")

    duplicate = report.get("duplicate_ratio", 0.0)
    if duplicate:
        lines.append(f"Дублирует уже написанное: {duplicate * 100:.0f}%")

    for note in report.get("notes") or []:
        lines.append(f"— {note}")

    if not report.get("measured_quality", False):
        lines.append("— качество ответа офлайн не измеряется (модель в сценариях скриптована)")

    return "\n".join(lines)
