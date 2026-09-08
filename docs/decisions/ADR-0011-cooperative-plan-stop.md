# 0011: Cooperative plan stop and fenced cleanup

**Date:** 2026-09-08
**Status:** accepted

## Context

ADR-0010 owns a plan's conversation across claim, turn linkage, continuation and
step outcome. Its entry checks prevent resuming an already cancelled/expired plan,
but do not revoke a running invocation's next tool/model call. Marking all steps
failed immediately also hides a live executor or a pending approval/effect.

An external request may already be in flight when a stop arrives. Neither a
cancelled asyncio waiter nor a failed local step proves that the request was undone.
Memory workers previously outlived the invocation's ownership without inheriting
its revocable authority.

## Decision

1. Persist the stop request separately: `plans.stop_reason` and
   `plan_steps.stop_reconciled` are additive v006 migration fields. Cancel records
   `plan_cancelled`; expiry records `plan_expired`. Ordinary failed plans do not
   acquire a stop request merely because their old TTL subsequently passes.
2. Inherit a composed execution guard through contextvars. Fresh plan turns,
   approval continuations and resume check current plan/turn authority at model
   and tool boundaries. Check again inside a queued tool task, before dispatch.
   Provider fallback attempts, synchronous custom-pipeline model factories,
   direct topic tools and knowledge pipeline writes honor this scope.
3. A stop is cooperative, not preemptive. A call authorized at the boundary is
   in flight; it may finish after the request. Persist its proven effect result
   before checking the next boundary. Unknown outcomes retain pending intents
   and require review. A stop between intent reservation and dispatch is also
   conservatively uncertain; pending is not proof that dispatch occurred.
4. Keep plan memory retrieval/persistence/compaction under the same scope and
   conversation owner. Durable linkage precedes retrieval/embedding calls.
   Their synchronous workers copy context and are awaited
   until exit, even after repeated cancellation. They cannot be killed by
   cancelling the waiter. This does not remove synchronous I/O from all other
   agent/custom-pipeline paths (F15 remains open).
5. Reconcile stopped steps in bounded, oldest-first batches under the same
   nonblocking conversation ownership. Reattach claim/link crash windows by
   durable caller identity. Close pending approvals and fail incomplete turns
   without deleting journals/cache/effects. Preserve proven completed results,
   refusals, legacy/unknown evidence and pre-existing needs_review states.
6. Persist uncertainty in the turn transaction as well as the eventual plan
   acknowledgement, so a crash between the two databases cannot erase the
   reason for review after an inconsistent pending approval has been closed.
   Reconciliation never executes a model or retries an external operation.
7. API/UI/CLI distinguish stop requested, cleanup pending and review required.
   Expired-plan summaries wait for cleanup and are deterministic, not fresh
   model/tool work. Ordinary completed summaries use a separate ephemeral
   `plan-summary:` thread; `plan:` invocations require durable linkage.

## Alternatives

### Immediately mark all steps failed or cancel every task

Rejected: the executor or synchronous worker can still be alive, and an upstream
request can succeed after local cancellation. This would misreport completion of
stopping, prematurely release ownership and destroy evidence.

### Poll process-local cancellation flags only

Rejected: API, CLI, poller and executor can live in different processes. Durable
plan authority must be re-read; a process-local event is not the source of truth.

### Forcefully terminate workers, roll back or automatically retry pending effects

Rejected: third-party effects are not locally transactional. Forced termination
widens uncertainty, rollback requires domain-specific compensation, and automatic
retry can duplicate a real external action. Those need operator reconciliation
and provider idempotency, not a fabricated generic success/failure decision.

## Consequences

### Positive

- New model/tool work stops at revocation boundaries across nested execution.
- Already dispatched work is not silently described as undone.
- Cleanup cannot race a live compliant owner; crash/restart does not retry the
  stopped plan or overwrite the durable review requirement.
- The UI communicates incomplete stopping and does not offer waiting-step resume
  on a closed plan.

### Negative

- Additional small SQLite reads occur at execution boundaries.
- A stuck synchronous SDK can delay cancellation/cleanup until its own timeout
  or process exit. Process isolation and comprehensive async I/O remain work.
- Stop notification delivery, operator reconciliation UX, retention and the full
  non-engine writer inventory are not solved by this boundary protocol.

### Neutral

- This is not a security sandbox for arbitrary plugins or proof of exactly-once.
  A subrequest inside an already dispatched SDK call remains in-flight work.
- The wrapper covers the model APIs used here: invoke, ainvoke and bind_tools;
  adding streaming/batch APIs requires explicit boundary coverage and tests.
- Durable outbox for progress, summaries and final cancellation notifications is
  still F09. A stored summary alone does not prove receipt by the owner.
- Production rollout requires all executors to use the ownership/stop protocol;
  no mixed old/new executors may be assumed safe.

## References

- [0010: Plan execution recovery](ADR-0010-plan-execution-recovery.md)
- [0008: Conversation execution ownership](ADR-0008-conversation-execution-ownership.md)
- [0007: External effect intents](ADR-0007-external-effect-intents.md)
- `tests/test_plan_stop.py`, `tests/test_execution_control.py`
- `tests/test_plan_kill.py`: stop-before-turn, stop-before-link and
  stop-after-effect/pending-intent SIGKILL cases.
- `docs/reviews/2026-09-08-remediation.md`: full scope and production gates.
