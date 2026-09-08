# ADR-0007 — Reserve external effects before dispatch

- **Status:** accepted
- **Date:** 2026-09-08

## Context

F08 recovery depends on F10: a post-factum effects ledger cannot tell an unsent
operation from an operation sent just before the process died. Swallowing ledger
or journal errors makes this worse. Direct approval execution and custom nested
ReAct agents did not consistently receive the persistence callbacks.

## Decision

Introduce an additive `effect_intents` ledger. An IMMEDIATE transaction reserves
an operation with an unguessable owner token before invoking the tool. It records
the owning turn, tool, frozen arguments and argument fingerprint. A second
transaction atomically publishes the result and closes the intent, only for that
token. Legacy recorded results remain reusable; the legacy result-import helper
cannot overwrite an intent or substitute for the reservation protocol.

A pending intent is evidence of possible dispatch, not evidence of failure. It
never ages into retry permission. It blocks further writes in that turn and the
same tool/argument operation in another turn. A committed result with a lost
acknowledgement is reused on retry. A timeout, cancellation, exception or failed
result commit retains uncertainty. Turn finalization and automatic resume reject
unresolved intents even if a model would otherwise claim success.

Engine-managed mutations require a durable intent context. Approved tools use it
too. Nested ReAct loops inherit the parent's effect scope through a scoped
ContextVar, including custom functions that do not accept callback kwargs.
Journal and tool-cache failures propagate instead of allowing execution to
continue. Mutation classification includes MCP annotations and legacy built-in
action names independently of the approval-prompt toggle.

Pending intents are included in operator inspection and excluded from retention.
Identical arguments under a different call id may be a second intended purchase,
not a retry. The default key therefore stops for intent review instead of
silently collapsing two operations into one. An explicitly declared business key
can authorize deduplication across regenerated call ids. Supporting distinct
identical operations without ambiguity is still a required F10 follow-up.

Arguments remain in the private per-agent database, not in error messages or
logs. Rollout must preserve the production audit's permissions/backup gates.

## Alternatives

- Record only after dispatch: cannot close the crash window.
- Retry a timed-out tool: its receiver may have applied the operation already.
- Expire a reservation automatically: a slow original process or worker thread
  can still finish; time alone cannot prove it is safe to repeat.
- Pretend every provider supports an idempotency header: many tools and MCP
  servers expose no such contract. No invented upstream guarantees.
- Swallow errors to keep the model running: loses the evidence needed for safe
  recovery and allows further writes after an unknown outcome.

## Consequences

### Positive

- A process killed after a real effect but before result commit cannot silently
  dispatch that operation again through the protected engine path.
- Legacy writes and approved/nested calls share the durable boundary.
- Operators can distinguish recorded results from unresolved intents and inspect
  the frozen parameters without reconstructing a child's regenerated prompt.

### Negative

- Even a failure that actually happened before delivery can require review: the
  generic tool interface cannot prove a remote operation did not happen.
- Ephemeral callers can no longer perform engine-managed mutations without a
  durable context. Hermetic evals use an isolated temporary SQLite ledger.
- Frozen parameters add sensitive state to the already private session database.

### Neutral

- This is the first F10 stage, not an exactly-once claim or full issue closure.
  Provider-specific idempotency/reconciliation, operator resolution with live
  ownership checks, and inventory of direct calls outside the engine remain.
- F08/F11 still need live execution ownership and recovery. A reservation token
  fences result publication, not arbitrary provider side effects or a thread
  already executing inside an external SDK.
- A changed request in a new turn can represent a different operation; semantic
  duplicates require provider/business keys, not just argument equality.

## References

- `docs/reviews/2026-09-08-remediation.md` — F08–F11
- `tests/test_effect_intents.py` — real SQLite fault injection and concurrency
- `tests/test_durable_kill.py` — real SIGKILL before effect-result commit
