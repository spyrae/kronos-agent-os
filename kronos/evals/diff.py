"""Compare suite results across two revisions.

The question this answers: "I changed the persona / a policy / the engine — what
moved?" Comparison is deliberately structural. Untrusted framing carries a random
boundary id, model wording is not a spec, and prose diffs of an LLM answer are
noise; what is stable and meaningful is which tools ran, how many turns it took,
which checks failed, and whether the answer changed size materially.

Running the base revision uses a temporary git worktree with the CURRENT
scenarios, so the code differs between runs and the yardstick does not.
"""

import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("kronos.evals.diff")

KIND_NEW_FAILURE = "new_failure"
KIND_FIXED = "fixed"
KIND_TOOLS_CHANGED = "tools_changed"
KIND_TURNS_CHANGED = "turns_changed"
KIND_ANSWER_CHANGED = "answer_changed"
KIND_APPROVALS_CHANGED = "approvals_changed"
KIND_ADDED = "scenario_added"
KIND_REMOVED = "scenario_removed"

# Answers are LLM prose; only a material size change is worth reporting.
_ANSWER_TOLERANCE = 0.10


class DiffError(Exception):
    """Raised when a comparison cannot be performed."""


@dataclass
class DiffEntry:
    scenario: str
    kind: str
    detail: str

    def to_dict(self) -> dict:
        return {"scenario": self.scenario, "kind": self.kind, "detail": self.detail}


@dataclass
class DiffReport:
    base_ref: str
    head_ref: str
    entries: list[DiffEntry] = field(default_factory=list)
    base_failed: int = 0
    head_failed: int = 0

    @property
    def regressions(self) -> list[DiffEntry]:
        return [entry for entry in self.entries if entry.kind == KIND_NEW_FAILURE]

    @property
    def empty(self) -> bool:
        return not self.entries

    def to_dict(self) -> dict:
        return {
            "base_ref": self.base_ref,
            "head_ref": self.head_ref,
            "base_failed": self.base_failed,
            "head_failed": self.head_failed,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    def render_markdown(self) -> str:
        if self.empty:
            return f"**No behaviour change** between `{self.base_ref}` and `{self.head_ref}`."

        lines = [
            f"### Behaviour diff `{self.base_ref}` → `{self.head_ref}`",
            "",
            f"Failing scenarios: {self.base_failed} → {self.head_failed}",
            "",
            "| Scenario | Change | Detail |",
            "|---|---|---|",
        ]
        for entry in sorted(self.entries, key=lambda item: (item.kind != KIND_NEW_FAILURE, item.scenario)):
            lines.append(f"| {entry.scenario} | {entry.kind} | {entry.detail} |")
        if self.regressions:
            lines.extend(["", f"⚠️ {len(self.regressions)} new failure(s)."])
        return "\n".join(lines)


def _by_name(report: dict) -> dict[str, dict]:
    return {str(entry.get("name")): entry for entry in report.get("scenarios") or []}


def _failed_names(report: dict) -> set[str]:
    return {str(entry.get("name")) for entry in report.get("scenarios") or [] if entry.get("status") != "pass"}


def diff_reports(base: dict, head: dict, *, base_ref: str = "base", head_ref: str = "head") -> DiffReport:
    """Compare two suite reports produced by the runner."""
    base_scenarios, head_scenarios = _by_name(base), _by_name(head)
    base_failures, head_failures = _failed_names(base), _failed_names(head)
    report = DiffReport(
        base_ref=base_ref,
        head_ref=head_ref,
        base_failed=len(base_failures),
        head_failed=len(head_failures),
    )

    for name in sorted(set(base_scenarios) | set(head_scenarios)):
        before, after = base_scenarios.get(name), head_scenarios.get(name)
        if before is None:
            report.entries.append(DiffEntry(name, KIND_ADDED, "not present in base"))
            continue
        if after is None:
            report.entries.append(DiffEntry(name, KIND_REMOVED, "not present in head"))
            continue

        if name in head_failures and name not in base_failures:
            report.entries.append(DiffEntry(name, KIND_NEW_FAILURE, _failure_detail(after)))
        elif name in base_failures and name not in head_failures:
            report.entries.append(DiffEntry(name, KIND_FIXED, "now passes"))

        before_metrics = before.get("metrics") or {}
        after_metrics = after.get("metrics") or {}

        before_tools = list(before_metrics.get("tools_called") or [])
        after_tools = list(after_metrics.get("tools_called") or [])
        if before_tools != after_tools:
            report.entries.append(
                DiffEntry(name, KIND_TOOLS_CHANGED, f"{before_tools or 'none'} → {after_tools or 'none'}")
            )

        before_approvals = sorted(before_metrics.get("approvals_requested") or [])
        after_approvals = sorted(after_metrics.get("approvals_requested") or [])
        if before_approvals != after_approvals:
            report.entries.append(
                DiffEntry(
                    name,
                    KIND_APPROVALS_CHANGED,
                    f"{before_approvals or 'none'} → {after_approvals or 'none'}",
                )
            )

        before_turns = int(before_metrics.get("model_turns") or 0)
        after_turns = int(after_metrics.get("model_turns") or 0)
        before_calls = int(before_metrics.get("tool_calls") or 0)
        after_calls = int(after_metrics.get("tool_calls") or 0)
        if (before_turns, before_calls) != (after_turns, after_calls):
            report.entries.append(
                DiffEntry(
                    name,
                    KIND_TURNS_CHANGED,
                    f"model turns {before_turns}→{after_turns}, tool calls {before_calls}→{after_calls}",
                )
            )

        before_chars = int(before_metrics.get("answer_chars") or 0)
        after_chars = int(after_metrics.get("answer_chars") or 0)
        if _materially_different(before_chars, after_chars):
            report.entries.append(
                DiffEntry(name, KIND_ANSWER_CHANGED, f"answer length {before_chars} → {after_chars} chars")
            )

    return report


def _failure_detail(entry: dict) -> str:
    if entry.get("error"):
        return f"error: {entry['error']}"
    failed = [check.get("name") for check in entry.get("checks") or [] if not check.get("passed")]
    return f"failed checks: {', '.join(str(name) for name in failed)}" if failed else "failed"


def _materially_different(before: int, after: int) -> bool:
    if before == after:
        return False
    if before == 0 or after == 0:
        return True
    return abs(after - before) / max(before, after) > _ANSWER_TOLERANCE


def _git(args: list[str], *, cwd: Path) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DiffError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def run_suite_at_ref(ref: str, *, suite_dir: Path, repo_root: Path, python: str = "") -> dict:
    """Run the suite against the code at ``ref`` in a throwaway worktree.

    The suite path stays absolute and points at the CURRENT scenarios: the
    comparison is of code, so the yardstick must not move with it.
    """
    import sys

    interpreter = python or sys.executable
    workdir = Path(tempfile.mkdtemp(prefix="kaos-eval-base-"))
    worktree = workdir / "tree"
    try:
        _git(["worktree", "add", "--detach", str(worktree), ref], cwd=repo_root)
        output = workdir / "report.json"
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                interpreter,
                "-m",
                "kronos.cli",
                "eval",
                "run",
                "--suite",
                str(suite_dir.resolve()),
                "--json",
                str(output),
            ],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            check=False,
            env=_child_env(worktree),
        )
        if not output.exists():
            stderr = (result.stderr or result.stdout).strip()
            if "invalid choice: 'eval'" in stderr:
                # Comparing against a revision from before this feature existed.
                raise DiffError(
                    f"revision {ref} predates `kaos eval` and cannot run the suite — "
                    "save a report from a newer revision and pass it with --base-json"
                )
            raise DiffError(f"running the suite at {ref} produced no report: {stderr[:400]}")
        return json.loads(output.read_text(encoding="utf-8"))
    finally:
        try:
            _git(["worktree", "remove", "--force", str(worktree)], cwd=repo_root)
        except DiffError as e:
            log.warning("Could not remove eval worktree: %s", e)
        shutil.rmtree(workdir, ignore_errors=True)


def _child_env(worktree: Path) -> dict:
    """Environment for the base-revision run: hermetic and key-free."""
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(worktree)
    for key in list(env):
        if key.endswith(("_API_KEY", "_TOKEN")):
            env.pop(key, None)
    return env
