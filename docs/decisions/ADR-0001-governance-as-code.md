# ADR-0001 — Governance as code: `policy.yaml`

- **Status:** accepted
- **Date:** 2026-07-25
- **Context:** moat roadmap phase 9.3

## Problem

"What is this agent allowed to do?" had no single answer. The rules lived in six
environment flags (`kronos/config.py`), three constant lists in `kronos/engine.py`,
two module constants in `kronos/security/cost_guardian.py`, and an injection
reaction setting. Answering the question required reading four files, and
answering it *for an auditor* required trusting that reading.

## Decision

One declarative file, `policy.yaml`, validated by `kronos/policy.py`, covering
capability gates, approval rules, budgets, untrusted-output handling, egress,
retention and PII masking. Absent the file, behaviour is unchanged.

### Precedence: env > policy > code default

The policy file is declared intent. An explicit environment variable is an
operator overriding that intent for one deployment, so the override wins —
otherwise a hotfix applied through env would silently do nothing, which is worse
than not supporting overrides at all. `kaos policy report` prints the winning
source for every value so an override is never invisible.

"Explicit" is detected by comparing the live setting against the pydantic field
default. By the time policy loads, `pydantic-settings` has already merged env,
`.env` and defaults into one object; comparing against the declared default is
the only honest way to distinguish an override from a default without reparsing
the environment. The trade-off: an operator who sets an env var to exactly the
default value is indistinguishable from one who set nothing. That case changes
no behaviour, so it is accepted.

### Policy is pushed into `settings`, not read at every call site

`apply_to_settings()` mutates the global settings object once at startup instead
of teaching every gate-reading module about the policy. Two reasons:

1. Capability gates are read in a dozen places (`tools/`, `agents/`, `dashboard/`,
   `cli.py`). Rewriting all of them for one feature is a large, risky diff for no
   behavioural gain.
2. The pattern already exists: `cli._force_demo_safety()` mutates the same fields
   to keep demo mode conservative.

Approvals and budgets are the exception — they read the policy at call time,
because tests and `/persona`-style runtime changes must take effect without a
restart.

### Fail closed on a malformed policy

Startup (`app._activate_policy_or_exit`) exits 1 on an invalid file. A policy that
cannot be parsed must not silently degrade into permissive defaults — the same
reasoning as the webhook secret, where an empty secret returns 401 instead of
accepting everything.

Mid-run callers (`get_policy()`) log the error and fall back to defaults instead
of raising: a cron job should not crash the agent because someone edited the file
while it was running. Startup is where a bad file stops the process.

### Empty lists mean "unspecified", not "empty"

`approvals.always: []` keeps the engine defaults. A blank YAML list is far more
likely to be an omission than a deliberate decision to gate nothing, and reading
it as the latter would silently un-gate deploys and expense writes.

## Consequences

- One file to read, review and diff; `kaos policy report` makes the effective
  posture printable.
- Secrets stay out: keys remain in `.env`, and the policy references none.
- The `_SETTINGS_MAP` table in `kronos/policy.py` is the single place where a new
  policy key gets wired to a setting; adding a key in two places would let them
  drift.
- A future multi-user deployment will need per-user policy, which this design
  does not attempt — it is deliberately one posture per process.
