# ADR-0005 — Explicit and durable invocation outcomes

- **Status:** accepted
- **Date:** 2026-09-08

## Context

Review F07 found that plan steps treat any nonempty agent response as completion,
including an approval prompt. Engine failures also return human-readable text.
The mutable last-approval property cannot correlate concurrent calls. A completed
turn deletes its journal; shared history can advance or be compacted before a
plan observes an approval continuation's result.

## Decision

Add a call-local immutable `InvocationOutcome` and `ainvoke_outcome`; retain
`ainvoke` as a text-only compatibility wrapper. Approval waits, input blocks,
engine failures and normal completion have distinct states. Completion describes
execution, not proof of the model's claims or every external operation's success.

Persist the exact terminal content with the turn's state in the same transaction
as session history and journal cleanup. An additive migration leaves old rows
NULL: missing evidence must not be inferred from a newer conversation response.
Read turn and approval state in one SQL snapshot. A claimed decision remains
running until its continuation terminates; a rejected decision is not success.

Expose a synchronous `on_turn_started` hook to durably correlate caller work
before any model/tool invocation. Linking failures stop the invocation. Plans
will use the turn identifier for reconciliation instead of retrying a prompt
merely because the process or approval UI returned.

## Alternatives

- Match response text or consult `last_pending_approval_id`: wording-dependent,
  racy between calls, and unavailable after restart.
- Change `ainvoke` to return an object: breaks existing transports and callers.
- Recover the answer from history or journal: journal is deleted on completion;
  history is shared and can be compacted or cleared.
- Store only an in-memory future: cannot reconcile an approval after restart.
- Link only after invocation returns: leaves a crash window after effects but
  before the plan learns which turn performed them.

## Consequences

### Positive

- Execution consumers can distinguish approval, failure and completion without
  parsing human text or sharing mutable per-agent state.
- Outcomes survive journal cleanup, history advancement and process restart.
- Legacy data is retained and unknown outcomes fail closed for reconciliation.

### Negative

- Exact final text adds storage proportional to retained turns.
- The text-only API remains unsuitable for execution decisions; callers must
  migrate deliberately, not assume the wrapper solved their state handling.

### Neutral

- Plan integration and approval delivery are the next F07 step, not complete in
  this foundational change. Crash ownership, reliable delivery and effect
  reconciliation remain F08/F09/F10/F11.
- A failed engine turn may already have performed effects. Its failure state is
  not permission for automatic replay.

## References

- `docs/reviews/2026-09-08-remediation.md` — F07–F11
- `tests/test_invocation_outcomes.py` — migration and execution regression tests
