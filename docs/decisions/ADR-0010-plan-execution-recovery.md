# 0010: Plan-owned execution recovery across two SQLite databases

**Date:** 2026-09-08
**Status:** accepted

## Context

F07 linked a step to its turn before model calls but still left a crash window:
plan claim and turn creation live in separate SQLite databases. A crash between
`begin_turn` and the callback could leave a running step with no reverse link.
Blindly starting another turn loses its original effect identity. Separately,
claiming before the conversation lock made a live waiting executor look abandoned.
F08 therefore needs actual caller recovery, not only a durable engine API.

## Decision

The plan poller obtains the ADR-0008 conversation ownership **before** claiming a
step and keeps it through invocation/outcome persistence. It lends the task-bound
capability to `KronosAgent`; locks are not made globally reentrant. An agent without
its durable store cannot execute plan steps. Mismatched storage configuration
fails ownership validation instead of silently using an independent lock.

Migration v005 gives each new execution a stable caller key, stored in the step
claim and in the same transaction as `active_turns` insertion. A partial unique
index enforces at most one turn for that key. The reverse link uses compare-and-set
against the key. Recovery can reattach from turn metadata without decoding a
potentially damaged journal. Legacy unlinked executions have no proof of what ran
and become `needs_review`, never an automatic fresh attempt.

A claim with no turn is retried with a 60-second backoff and the existing attempt
cap. **It retains the same key across pre-turn retries.** A delayed commit can
therefore be reattached or rejected by the unique index, not create a second turn
under a replacement identity. Generic retention keeps caller-owned rows: deleting
the only correlation record would make absence look like proof of no execution.
A safe compact archive/tombstone policy remains required before claiming bounded
retention for all caller-owned history.

Generic startup recovery excludes plan/caller-owned turns. The poller repairs
correlation first and then follows `durable.resume_mode`. In report mode the step
becomes `interrupted` without a model call. It remains visible, blocks other steps
of that plan and can be explicitly continued using the same turn from CLI/API/UI.
Resume mode invokes the existing intent/journal-protected continuation. Unknown
outcomes and pending effects do not permit a replacement turn.

Recovery is bounded and fair, skips live ownership and shares the per-cycle
execution budget with fresh steps. A recovered plan cannot immediately consume a
second execution slot for its next step in the same cycle. Candidate state is
re-read after ownership: a stale snapshot must not reclassify a newly parked step.

A live linked step's park request is persisted separately from execution state.
Completion atomically stores the result and either finishes the step or detaches
the completed turn into `last_turn_id` and waits on the new condition. A linked
legacy waiting state is preserved through approval/interruption transitions.
Release refuses live/unresolved/cancelled/expired work and returns an actual CAS
result; dashboard/CLI no longer report releases that did not happen. A late
negative condition check cannot repark a released/live step. Corrupt condition
JSON requires review rather than consuming execution slots indefinitely.

## Alternatives

- **Reset all running steps to pending on startup:** rejected; may duplicate real
  effects and race a live executor.
- **Use only timestamps / expiring leases:** rejected for this single-host runtime;
  an event-loop stall is not proof that the executor died. Kernel ownership already
  provides that distinction.
- **Merge the two databases now:** possible longer-term, but it would migrate an
  unrelated storage boundary and still require exclusive execution and upstream
  effect reconciliation. A durable unique correlation solves this window without
  that wider migration.
- **Fresh correlation after each pre-turn failure:** rejected; a delayed commit
  could escape deduplication.
- **Always auto-resume regardless of policy:** rejected; report-only is an explicit
  operational choice, not authorization to invoke models/tools after a restart.

## Verification and remaining scope

Tests cover two database commit windows, cancelled tasks, live ownership, stable
retry keys and delayed commits, report/manual continuation, legacy/mismatched
claims, pending intents, cycle quotas, park/release races, retention and migration.
Four new integration cases SIGKILL real processes after claim, before reverse link,
during model work and after turn completion before step persistence. A fresh
process completes one original turn; a second recovery does not call the model.
Model work interrupted before its result may run again; this is not an exactly-once
model or external-action promise. Existing durable effect SIGKILL tests remain.

F08 is still open for cooperative **live cancellation/expiry fencing**, owner
notifications, cleanup of expired approvals, and operator reconciliation of
`needs_review`. F09 delivery outbox, F10 child/direct pipeline coverage and bounded
business-key/caller-identity retention also remain open. No production rollout or
configuration changes are part of this decision; all old executors must be drained
before activating the ownership-based protocol in production.
