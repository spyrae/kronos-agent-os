# Engineering quality methodology

**Version:** 1.0
**Effective date:** 2026-07-13

This document fixes the measurement boundary for the Engineering Quality
Dashboard. The boundary must not be changed to improve a metric. Any future
change needs a new methodology version, a rationale, and an explicit break in
the metric history.

## Scope

All paths are relative to the repository root.

### Core product code

Core coverage and maintainability measurements include:

```text
kronos/**
dashboard/api/**
aso/**
```

The following paths are excluded from core measurements:

```text
tests/**
.venv/**
**/migrations/**
**/__pycache__/**
```

The dashboard UI has its own Node.js pipeline and is not combined with Python
quality metrics. Dependencies, generated files, build artefacts, local data,
and virtual environments are never part of the source-code denominator.

## Metric definitions

| Metric | Definition |
| --- | --- |
| Tests | Passed and failed tests from `pytest -m "not integration"`; deselected and skipped tests are recorded separately. |
| Core coverage | Line coverage from `pytest-cov` over `kronos`, `dashboard`, and `aso`, restricted to the core paths above. It is not a whole-repository percentage. |
| Static blocking | Findings from `ruff check` in production code. Undefined names (`F821`) are blocking. |
| Format compliance | `ruff format --check` over tracked Python code in the measurement scope. |
| Type errors | Findings from the configured `mypy` invocation. A missing or failed type run is reported as unavailable, never as zero. |
| Vulnerabilities | High and critical findings from `pip-audit` for the locked Python environment. The tool, scope, date, and result are included in every snapshot. |
| Security findings | Bandit findings for production code only; `tests/` is excluded because test assertions are not production vulnerabilities. |
| Complexity | Not automated in the CI collector; maintainability is reviewed offline and is not shown as a headline metric (displayed as unavailable). |
| Duplication | JSCPD result over the core paths; generated and dependency files remain excluded. |

## Reproducibility and publication

`scripts/quality_report.py` is the canonical collector. It records the tool
versions, UTC timestamp, commit SHA, commands, scope, and normalized metrics
in `quality.json`. Raw command output remains in CI artifacts and is not
published to avoid exposing paths, source fragments, or operational data.

A snapshot may be labelled **CI-generated** only when a successful CI run
created it for the displayed commit. Locally generated snapshots are labelled
as local verification. A zero-vulnerability claim is valid only when the
snapshot includes a successful `pip-audit` result for the locked dependency
scope.

## Remediation rules

- Hash usage is reviewed call by call. `usedforsecurity=False` is allowed only
  for a documented non-security use such as a cache key or deterministic
  fingerprint; it is never used as a blanket Bandit suppression.
- Type, lint, and security findings are fixed at their cause. Broad ignores,
  `# type: ignore`, and baseline files that hide findings are not used to
  improve reported metrics.
- Tests must execute production behaviour. Trivial tests written only to raise
  coverage are out of scope for quality work.
- Coverage is a non-regression CI gate from the first verified core baseline;
  the long-term target is tracked separately from the gate.
