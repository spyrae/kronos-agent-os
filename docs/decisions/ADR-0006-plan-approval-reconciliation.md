# ADR-0006 — Reconcile plan approvals by durable turn

- **Status:** accepted
- **Date:** 2026-09-08

## Context

F07 requires a plan step to remain unfinished while an operation awaits approval.
ADR-0005 introduced execution outcomes, but the plan poller still interpreted
strings as completion. Plans use isolated `plan:<id>` execution threads, not the
owner's Telegram chat; ordinary webhook notifications had no approval controls.

## Decision

Claim a step atomically, persist its turn link before model calls, and reconcile
that exact turn from the durable outcome API. Add `awaiting_approval` and
`needs_review` states. Normal completion with content is a step result; refusal
or expiration fails the step. Failures after a turn starts, missing evidence and
empty terminal replies require review rather than a new turn with a new effect
idempotency scope. An exception before any durable turn may still retry.

Only one unresolved execution may own a plan thread. Paused plans are excluded
from the ready queue so they cannot starve other plans. Reconciliation is bounded
and rotates by last check time; it runs before summary delivery and ready steps.

Persist the last successfully delivered approval id. Retry a failed approval
notification without re-invoking the operation. Delivery resolves destination and
arguments from the linked plan and durable approval, not arbitrary webhook fields.
Always include `/approve <id>` and `/reject <id>` for userbot accounts. Bot
accounts can additionally use inline buttons. Oversized arguments require CLI
inspection rather than an approval invitation with hidden arguments.

Commands and plan callbacks require an explicit owner allowlist and the stored
chat/topic. Broad chat access does not grant approval authority. Agent approval
and resume entry points reject cancelled/expired/unlinked plan turns. Approval
claims remain the existing SQLite atomic decision primitive.

## Alternatives

- Mark a pause failed and retry: asks for approval repeatedly and can duplicate
  earlier effects under a new turn id.
- Keep the step merely running with no turn link: neither restart nor a later
  callback can identify the result to attach.
- Use only inline keyboards: Telegram user accounts do not support them.
- Route approval through the normal model: adds ambiguity, prompt injection and
  model cost to an authorization decision.
- Infer that a claimed Approve means success: the continuation can fail or ask
  for another approval, so its terminal outcome remains authoritative.

## Consequences

### Positive

- Approve/Reject and restart recovery advance the matching step, not another
  concurrent call. A prompt is never stored as successful completion.
- Notifications reach userbot deployments as actionable commands.
- Cancelled plans cannot be revived through a stale pending approval.

### Negative

- Unknown/failed executions stop in needs_review; the operational review and
  recovery workflow is remaining F08 work, not automatic replay.
- Approval notification acknowledgement is at-least-once: a crash after Telegram
  accepts a message but before SQLite acknowledges it can duplicate the prompt.
  General delivery outbox and summary guarantees remain F09.

### Neutral

- Existing failed-dependency semantics remain: a later step may handle a stated
  refusal/absence, but an unresolved approval blocks the whole plan thread.
- Live operation cancellation, legacy orphaned steps, expired approval cleanup
  and full crash ownership remain F08/F11. These guards do not recall an effect
  already authorized and started.
- No claim of exactly-once external effects; F10 remains open. Transport tests
  mock Telegram; production rollout and live smoke are separate gates.

## References

- [ADR-0005](ADR-0005-durable-invocation-outcomes.md)
- `docs/reviews/2026-09-08-remediation.md` — F07–F11
- `tests/test_invocation_outcomes.py`, `tests/test_plan_poller.py`
