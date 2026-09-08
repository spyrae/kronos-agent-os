# ADR-0015: Budget admission at model dispatch

**Date:** 2026-09-08
**Status:** accepted

## Context

F12 found that admission ran only in the Telegram message handler. Cron, direct
invocations and recovery could call the same providers without that check. The
supervisor captured its model at construction and ignored force_tier. Merely
changing model selection during construction would leave long-lived specialists
on their original provider after spending crossed a downgrade threshold.

## Requirements

- Every invocation through the shared model factory checks current accounting.
- Every explicit provider fallback attempt checks again before dispatch.
- Cached models and their tool bindings respect a call-time lite constraint.
- A child cannot undo an outer downgrade; concurrent tasks cannot share it.
- Missing/corrupt accounting must not be treated as free spend.
- Budget refusal is not a retriable provider error or successful execution.

## Decision

The factory wraps normal models in BudgetedModel. It admits invoke/ainvoke at
dispatch, selects the configured lite chain when required, and applies saved
tool bindings to that effective model. Metadata attributes remain available;
unhandled callable methods cannot bypass the wrapper via attribute forwarding.
Explicit fallback chains also check before each provider attempt and may switch
only to lite when the constraint tightens. SDK-internal retries remain below
this boundary and need accounting reservations before a hard cap can be claimed.

A ContextVar scope carries force_tier through the supervisor and nested tasks.
The existing revocable execution scope still applies, including to models cached
before that scope was established. Factory tier inputs normalize strings to the
ModelTier enum rather than crashing on a string's missing value attribute.

Admission uses session_id or, if absent, thread_id from trusted audit context;
the callback uses the same fallback. Daily buckets use UTC on both write and
read. Invalid money values are rejected, and an unavailable daily ledger blocks
admission. The engine records budget_blocked instead of suggesting a generic
retry or treating the invocation as complete. Offline cassette replay does not
need a spending allowance because it does not dispatch a provider request.

## Alternatives

### Keep checks in every entry point

Rejected: new cron jobs and resumed/delegated tasks can omit the check. It also
cannot react to spending between consecutive steps of one ReAct loop.

### Replace cached models only when the agent is constructed

Rejected: the daily balance and request-specific force_tier change after startup.
Rebuilding the entire supervisor would also risk changing its tool surface.

### Treat this preflight check as an atomic dollar cap

Rejected: concurrent calls can all pass before any reports cost, SDK retries and
unknown outcomes may be billable, callbacks are still best-effort, and some SDK
paths bypass the factory. Reservations and reconciliation are separate required
work, not guarantees obtained from this wrapper.

## Consequences

### Positive

- Factory-backed paths share one admission boundary instead of Telegram-only logic.
- Runtime degradation preserves tool schemas/options and task isolation.
- Read failures no longer produce a false zero-spend admission decision.

### Negative

- The factory returns a proxy even for a single provider; raw concrete class
  identity and unguarded stream/batch/structured-output methods are not promised.
- Accounting reads are synchronous; F15 must include these reads.
- Direct Vision/Mem0 SDK calls, durable session scope/totals, reservations,
  crash/unknown-cost reconciliation and complete pricing remain open under F12.

### Neutral

- Personal-budget quiet-mode semantics and configured provider chains are unchanged.
- Existing local-day ledger rows are not relabelled automatically; non-UTC
  deployments require historical reconciliation around the changeover.
- No dependency/configuration/schema change or production rollout occurs here.

## Verification

Tests use real factory/guardian/SQLite with fake providers. They exercise
sync/async single and fallback calls, cached tool-bound models, force_tier task
isolation, budget exhaustion between ReAct steps, normal durable invocation and
resume, malformed/failed accounting and UTC bucket identity. These are local
regression checks, not provider billing or production acceptance.

## References

- `tests/test_model_budget.py`, `tests/test_cost_tracking.py`.
- `docs/reviews/2026-09-08-remediation.md`: F12 and remaining admission coverage.
- `kronos/llm.py`, `kronos/security/model_budget.py`.
