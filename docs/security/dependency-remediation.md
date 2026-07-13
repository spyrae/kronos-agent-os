# Dependency security remediation

**Reviewed:** 2026-07-13
**Tool:** `pip-audit` against the locked `uv` environment

The following vulnerable packages were upgraded to their first patched
versions. The lockfile remains the reproducible source for CI and local runs.

| Package | Previous | Patched | Advisory |
| --- | --- | --- | --- |
| `langsmith` | `0.8.14` | `0.8.18` | `GHSA-f4xh-w4cj-qxq8` |
| `pydantic-settings` | `2.14.1` | `2.14.2` | `GHSA-4xgf-cpjx-pc3j` |
| `starlette` | `1.3.0` | `1.3.1` | `PYSEC-2026-249` |

`pydantic-settings` is a direct dependency, so its lower bound is now
`>=2.14.2`. The other two are transitive dependencies retained at their
patched versions by `uv.lock`.

Verification used:

```bash
uv sync --locked --extra dev
uv run --with pip-audit pip-audit
uv run pytest -m "not integration" -q
```

The audit reported no known vulnerabilities, and the non-integration suite
passed after the update. Future CI must install with `uv sync --locked` so it
measures the reviewed dependency set.
