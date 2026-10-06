# ADR-0023: Model spend is reserved before dispatch and settled after

**Date:** 2026-10-06
**Status:** accepted

## Context

ADR-0015 put an admission check before every model call, but it compared the
call against spend already recorded. Concurrent calls each saw the same
remaining budget. The session total was a dictionary in process memory, so a
restart reset it and another agent process never saw it. A call that timed out
or was cancelled left nothing behind: its cost, if any, was simply absent.
Approval and resume charged the thread id, a different key from the chat id the
original turn was charged to.

## Decision

`swarm.db` holds two new tables. `session_costs` is the recorded spend of one
conversation per UTC day. `budget_holds` is money promised to calls that have
not reported their cost.

- **Reserve.** In one transaction: recorded spend + open holds + this call's
  estimate must fit the daily and the session limit, then the hold is inserted.
  SQLite orders the transactions of all agent processes.
- **Settle.** Recording a call's cost adds it to the day and the conversation
  and deletes the hold, in one transaction. If the write fails the hold stays.
- **Unknown.** A call that ends without a recorded cost — timeout, 5xx,
  cancellation, success without usable usage — keeps its hold, marked
  `unknown`, until the day ends. A 4xx refusal was not billed; its hold is
  released.
- **Turn scope.** `active_turns.budget_session` (v012) stores the conversation a
  turn is charged to. Approval and resume restore it.

The estimate is `budgets.reservation_usd` (default 0.02).

## Alternatives rejected

- **Reserve the model's maximum possible cost.** A worst case per call would
  block ordinary concurrency long before the limit is near.
- **Release a hold after a timeout.** A timeout does not prove the provider did
  not bill; holds end with the day, when they stop affecting any limit.
- **Keep the session total per process and persist it on shutdown.** A crash
  loses it, and another agent serving the same conversation never sees it.

## Consequences

- The cap bounds how far concurrent calls can overshoot by the difference
  between estimate and real cost; it is not an exact ceiling and not an invoice.
- A session cap now resets at midnight UTC, not on restart. A conversation that
  reached it stays blocked until then.
- During a provider outage with 5xx or timeouts, every failed attempt holds one
  estimate for the rest of the day. That is deliberate — the outcome is unknown —
  and with the default values about 250 such attempts exhaust a $5 day.
- Direct callers (ASO, GEO, Vision, Whisper, scripts) reserve through the same
  admission and settle through the same recorder; on failure nothing releases
  their hold, which is the conservative reading of an unknown outcome.
- Mem0 runs through this boundary but only against a fake: mem0ai is not
  installed locally. Acceptance against mem0ai 1.0.7 needs the package in a
  temporary environment, which is a dependency installation and has not been
  approved.
- No provider invoice is reconciled against these numbers.
