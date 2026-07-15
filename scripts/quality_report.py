#!/usr/bin/env python3
"""Generate a normalized engineering-quality snapshot for Kronos."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CORE_PATHS = ("kronos", "dashboard/api", "aso")
EXCLUDED_PATHS = ("tests/**", ".venv/**", "**/migrations/**", "**/__pycache__/**")


@dataclass(frozen=True)
class CommandResult:
    """Captured result of a quality-tool invocation."""

    returncode: int
    stdout: str
    stderr: str


def run_command(command: list[str], raw_dir: Path, name: str) -> CommandResult:
    """Run a command and persist its output without exposing it in the snapshot."""
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    (raw_dir / f"{name}.stdout.log").write_text(completed.stdout, encoding="utf-8")
    (raw_dir / f"{name}.stderr.log").write_text(completed.stderr, encoding="utf-8")
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def command_version(command: list[str], raw_dir: Path, name: str) -> str | None:
    """Return the first version-output line when the tool is available."""
    result = run_command(command, raw_dir, f"{name}_version")
    if result.returncode != 0:
        return None
    return result.stdout.strip().splitlines()[0] if result.stdout.strip() else None


def count_ruff_findings(output: str) -> int | None:
    """Extract Ruff's total finding count from its human-readable report."""
    match = re.search(r"Found (\d+) errors?", output)
    if match:
        return int(match.group(1))
    return 0 if output.strip() == "All checks passed!" else None


def count_mypy_findings(output: str) -> int | None:
    """Extract mypy's error count from its summary output."""
    match = re.search(r"Found (\d+) errors?", output)
    if match:
        return int(match.group(1))
    return 0 if "Success: no issues found" in output else None


def test_counts(output: str) -> dict[str, int | None]:
    """Extract pytest outcome counts without treating deselection as a skip."""
    counts: dict[str, int | None] = {"passed": 0, "failed": 0, "skipped": 0, "deselected": 0}
    for key in counts:
        match = re.search(rf"(\d+) {key}", output)
        if match:
            counts[key] = int(match.group(1))
    return counts


def coverage_percent(raw_dir: Path) -> float | None:
    """Read the total line coverage percentage from pytest-cov's JSON output."""
    coverage_file = raw_dir / "coverage.json"
    if not coverage_file.exists():
        return None
    try:
        payload = json.loads(coverage_file.read_text(encoding="utf-8"))
        return round(float(payload["totals"]["percent_covered"]), 2)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def duplication_percent(raw_dir: Path) -> float | None:
    """Run jscpd over the core paths and return the duplicated-lines percentage."""
    report_file = raw_dir / "jscpd" / "jscpd-report.json"
    try:
        run_command(
            [
                "npx",
                "--yes",
                "jscpd@5",
                "--silent",
                "--reporters",
                "json",
                "--output",
                str(raw_dir / "jscpd"),
                "--ignore",
                "**/tests/**,**/.venv/**,**/migrations/**,**/__pycache__/**",
                *CORE_PATHS,
            ],
            raw_dir,
            "jscpd",
        )
    except OSError:
        return None
    if not report_file.exists():
        return None
    try:
        payload = json.loads(report_file.read_text(encoding="utf-8"))
        return round(float(payload["statistics"]["total"]["percentage"]), 2)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def audit_metrics(raw_dir: Path) -> tuple[int | None, int | None]:
    """Run dependency and source audits, returning known and high findings."""
    audit = run_command(["pip-audit", "--format", "json"], raw_dir, "pip_audit")
    known_total: int | None
    try:
        dependencies = json.loads(audit.stdout)
        known_total = sum(len(item.get("vulns", [])) for item in dependencies.get("dependencies", []))
    except (AttributeError, json.JSONDecodeError):
        known_total = None

    bandit = run_command(
        [
            "bandit",
            "-r",
            "kronos",
            "dashboard/api",
            "aso",
            "--exclude",
            "tests,.venv",
            "--format",
            "json",
        ],
        raw_dir,
        "bandit",
    )
    try:
        payload = json.loads(bandit.stdout[bandit.stdout.index("{") :])
        findings = payload["results"]
        high_total = sum(finding["issue_severity"] == "HIGH" for finding in findings)
    except (KeyError, TypeError, json.JSONDecodeError):
        high_total = None
    return known_total, high_total


def main() -> int:
    """Generate `quality.json` and raw logs for the current commit."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("quality/quality.json"))
    arguments = parser.parse_args()

    output = arguments.output.resolve()
    raw_dir = output.parent / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    static = run_command(["ruff", "check", "."], raw_dir, "ruff_check")
    formatting = run_command(["ruff", "format", "--check", "."], raw_dir, "ruff_format")
    tests = run_command(
        [
            "pytest",
            "-m",
            "not integration",
            "--cov=kronos",
            "--cov=dashboard.api",
            "--cov=aso",
            f"--cov-report=json:{raw_dir / 'coverage.json'}",
            "-q",
        ],
        raw_dir,
        "pytest",
    )
    types = run_command(["mypy", "kronos", "dashboard/api", "aso"], raw_dir, "mypy")
    known_vulnerabilities, bandit_high = audit_metrics(raw_dir)
    duplication = duplication_percent(raw_dir)
    commit = run_command(["git", "rev-parse", "HEAD"], raw_dir, "git_commit")

    report: dict[str, Any] = {
        "project": "kronos-agent-os",
        "stack": "python",
        "visibility": "public",
        "measured_at": datetime.now(UTC).date().isoformat(),
        "commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "scope": {"include": list(CORE_PATHS), "exclude": list(EXCLUDED_PATHS)},
        "metrics": {
            "tests": test_counts(tests.stdout),
            "coverage_core_pct": coverage_percent(raw_dir),
            "static_blocking": count_ruff_findings(static.stdout),
            "format_compliant_pct": 100 if formatting.returncode == 0 else None,
            "vulns_critical_high": 0 if known_vulnerabilities == 0 and bandit_high == 0 else None,
            "vulnerabilities_known_total": known_vulnerabilities,
            "bandit_high": bandit_high,
            "type_errors": count_mypy_findings(types.stdout),
            "complexity_grade": None,
            "duplication_pct": duplication,
        },
        "tools": {
            "ruff": command_version(["ruff", "--version"], raw_dir, "ruff"),
            "pytest": command_version(["pytest", "--version"], raw_dir, "pytest"),
            "pip_audit": command_version(["pip-audit", "--version"], raw_dir, "pip_audit"),
            "bandit": command_version(["bandit", "--version"], raw_dir, "bandit"),
            "mypy": command_version(["mypy", "--version"], raw_dir, "mypy"),
            "jscpd": command_version(["npx", "--yes", "jscpd@5", "--version"], raw_dir, "jscpd"),
            "node": command_version(["node", "--version"], raw_dir, "node"),
        },
        "ci": {"required_gates": False, "badge_url": None},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
