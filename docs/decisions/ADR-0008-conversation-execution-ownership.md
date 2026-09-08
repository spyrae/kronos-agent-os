# 0008: Conversation execution ownership

**Date:** 2026-09-08
**Status:** accepted

## Context

F11 exposed a second executor in dashboard resume: it constructed a new agent
without the live MCP registry and treated `running` as proof of a crash. Startup
report recovery could also rewrite a live turn. A SQLite status CAS alone cannot
distinguish a dead executor from a slow one, and the previous batch claim left
`resuming` turns stranded after another crash. F08 needs this boundary before
plan-step recovery; F10 intents do not themselves provide execution ownership.

## Decision

Use a POSIX kernel `flock` sidecar per canonical SQLite path and conversation id.
Hold it across the complete durable invocation, approval continuation or resume,
including history load and finalization. Normal invocations/approvals wait without
blocking the event loop; manual/startup resume and report recovery refuse or skip
a busy conversation. No timeout transfers ownership to another executor.

Resume reads identity from SQLite, then claims one `running` or `resuming` turn
inside `BEGIN IMMEDIATE` while holding ownership. The claim checks pending
approvals, newer turns and the attempt cap. The caller cannot substitute a thread,
input or attempt count from a stale snapshot. Report recovery uses the same lock
and rechecks status before updating history. There is no database schema change.

Dashboard keeps its live agent in `app.state`, never constructing a fallback.
Without that agent resume returns 503; a live owner yields 409. Responses expose
the execution outcome so approval waits are not displayed as completed work.
Offline CLI resume opens and closes the managed MCP registry, using the same
guarded agent API. Completion counters require an actual completed outcome.

Sidecar names hash the thread id. New directories/files use 0700/0600 and file
descriptors are close-on-exec. Sidecars are not deleted after release: unlinking
and recreating a locked inode would split ownership. Closing a descriptor releases
its reference, including on SIGKILL; a forked child retaining a descriptor keeps
the lock until its close/exit/exec, conservatively preventing premature takeover.

## Alternatives

### SQLite timestamp lease with heartbeat

A stalled event loop or a long external request can outlive the lease. Without
provider-side fencing the old owner may still perform a write after takeover.
Expiry is therefore not safe proof that this single-host executor stopped.

### In-process locks or PID checks

An asyncio lock does not cover CLI or another process. PID checks introduce PID
reuse and check/use races. The kernel lock supplies the required lifetime without
a liveness polling protocol.

### Distributed lock service

Not introduced: the supported deployment already uses local SQLite on one host.
Multi-host execution would require a separate design for storage, fencing and
provider idempotency, not merely replacing this lock implementation.

## Consequences

### Positive

- Dashboard, chat, approvals and startup recovery cannot run one conversation
  concurrently when all executors use this protocol.
- A blocked event loop is not misclassified as a crash; SIGKILL releases ownership
  without an arbitrary wait for lease expiry.
- A second interrupted resume remains recoverable under the same attempt cap.
- No new dependencies, infrastructure service, runtime setting or DB migration.

### Negative

- A genuinely stuck process is not automatically taken over. An operator must
  stop it before recovery; F10 pending intents still require reconciliation.
- Lock sidecars must remain in place while any executor can run. Do not clean up
  or restore their directory over live processes.
- This is a cooperative application protocol, not a sandbox against arbitrary
  code with access to the SQLite file. Direct session/tool writers outside the
  agent API require separate inventory and integration under F08/F10/F13.

### Neutral

- Supported scope is POSIX, same host, canonical DB path and local filesystem;
  not Windows, NFS, hard-link aliases or distributed workers.
- Rollout must stop/drain old executors that do not acquire these locks before
  enabling recovery. No mixed-version safety is claimed.
- F08 plan-step lifecycle, F09 durable delivery and F10 effect reconciliation
  remain open. Exclusive execution is not exactly-once external delivery.

## References

- [Durable outcomes](ADR-0005-durable-invocation-outcomes.md)
- [Plan approvals](ADR-0006-plan-approval-reconciliation.md)
- [External effect intents](ADR-0007-external-effect-intents.md)
- [Audit remediation tracker](../reviews/2026-09-08-remediation.md)
- `tests/test_turn_ownership.py`, `tests/test_durable_kill.py`
