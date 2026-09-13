# 0013: Transactional recovery delivery

**Date:** 2026-09-08
**Status:** accepted

## Context

ADR-0012 introduced a producer-local outbox for plans. Session recovery still
finished a turn before invoking a transient delivery callback, and startup
waited for recovery before starting the bridge used by that callback. A crash
or unavailable self-webhook could leave a completed answer with no delivery duty.
Approval waits could also lose their notice, and were not successful answers.

Thread IDs alone cannot establish a Telegram destination: CLI callers may choose
numeric IDs too. Recovery must not send a private answer to a guessed recipient
or change the Telegram account after an uncertain send.

## Decision

1. Extend the shared outbox through migration v008 in the **session producer DB**.
   Add immutable original Telegram provenance (chat, topic, authenticated sender,
   whether outbound review is required), a recovery delivery-request flag, and an
   explicit delivery issue. Legacy rows keep empty provenance and no request.
   The bridge records provenance before the original durable model invocation;
   ephemeral peer reactions cannot carry it. Existing invocation parameters keep
   their order; the new optional argument is additive.
2. A user-facing resume claims its delivery duty in the same transaction as its
   execution claim, before any model or tool. Plan turns remain plan-owned and
   do not enqueue a second session notification. CLI/dashboard/startup resume use
   the same mechanism. Removing the post-finalization callback is deliberate: it
   cannot provide transactional delivery or safe crash recovery.
3. History, terminal outcome and its notification are committed together. Approval
   creation commits waiting state and its notice together. The request flag lasts
   through subsequent approval continuations and process restarts. Terminal
   results are immutable; a late failure cannot replace a completed answer or
   mutate its queue identity. Report-only recovery closes an already requested
   recovery duty with an interruption notice, without executing the model.
4. Queue frozen, redacted text with a recovery/turn identifier and stable MTProto
   chunk identities. The original model result remains separately available in
   the turn. Missing/invalid legacy provenance and absent legacy results are
   explicit states, not guessed destinations or fabricated delivered markers.
   An original owner answer requiring dissent remains `needs_review`; this stage
   does not silently bypass that gate to make delivery appear complete.
5. Mark obsolete approval notices with an additive tombstone, not a fake receipt.
   Terminalization and approval decisions suppress outdated work transactionally.
   A send already in flight can still arrive; its commands recheck durable state.
   Bounded expiry reconciliation holds conversation ownership before changing an
   expired recovery approval and queuing its failure notice. It never runs tools
   or adopts historical, non-recovery approvals automatically.
6. Recovery `/approve` and `/reject` require an explicitly allowlisted owner,
   the stored chat/topic and the original sending account. Callback checks use
   the actual event topic, not the stored topic as a substitute for evidence.
   A queued continuation is not also sent directly from the command/callback.
7. Startup creates bridge, dashboard, scheduler, delivery and recovery services
   concurrently, after installing shutdown handlers. Completing the one-shot
   recovery pass does not terminate healthy services. Shutdown cancels and joins
   the recovery task. The first incoming invocation cannot run a second,
   report-mode startup recovery against the concurrent resume pass.
8. Each outbox producer has an independent delivery loop. A blocked plan sender
   does not prevent the session queue from advancing; transient errors preserve
   the duty. The session queue uses the actual SessionStore path and closes its
   short-lived connection after each drain. Dashboard/CLI expose execution and
   delivery separately: delivered means transport acceptance, not human read.

## Alternatives

### Keep a callback and retry exceptions

Rejected: the completion-to-callback crash window remains, callback state is
lost on restart, and a false transport result can still be mistaken for success.
Retries must be driven by persistent duty, not by replaying the model.

### Put session notices into the plan database

Rejected: committing the session and enqueueing into another SQLite file would
reintroduce the two-database gap. Reusing the schema/protocol is sufficient;
the producer keeps its own transaction and database identity.

### Derive destinations from numeric thread IDs or adopt every old approval

Rejected: numeric identifiers are not authenticated transport provenance, and
historical pending rows may already represent unknown external outcomes.
Explicit operator adoption/reconciliation is safer than automatic guessing.

### Share one sequential delivery loop or await recovery before services

Rejected: a stalled transport or startup model would delay unrelated delivery
and bridge readiness. Separate supervised loops keep transport duties independent
without adding a broker, dependency, port, or configuration flag.

## Consequences

### Positive

- Process death cannot commit a recovered result without its valid delivery duty.
- Waiting approval is distinct from terminal execution and delivery.
- A completed result can be sent after restart without calling the model again.
- Recipient/account drift is rejected, and missing provenance is visible.
- Existing kernel ownership, chunk receipts and bounded retry contracts are reused.

### Negative

- Full F09 remains open: ordinary live replies, original non-recovery approvals,
  plan approval notices and other cron producers still need equivalent treatment.
- Operator route adoption, review resolution, safe reconciliation and queue
  retention remain required. Requested turns are not pruned meanwhile; this
  preserves evidence but is not a long-term storage policy.
- A dissent-required recovery result can remain blocked until operator review;
  this is explicit incomplete work, not successful delivery.
- SafeDB calls remain synchronous, as in ADR-0012; F15 must cover their event-loop
  impact along with the rest of model/memory/database I/O.

### Neutral

- Default report/resume policy and deployment configuration do not change.
- Telegram acceptance is not a read receipt or an unconditional exactly-once
  promise; uncertain provider identities still require reconciliation.
- No production migration, historical replay, external write or deployment is
  authorized by adding this local implementation.

## References

- ADR-0007: External effect intents.
- ADR-0008: Conversation execution ownership.
- ADR-0012: Transactional plan delivery.
- `docs/reviews/2026-09-08-remediation.md`: F09 and outstanding rollout gates.
- `tests/test_turn_delivery.py`, `tests/test_bridge_recovery.py`,
  `tests/test_recovery_startup.py`, `tests/test_turn_delivery_kill.py`.
