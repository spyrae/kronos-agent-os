# 0009: Durable tool-batch replay before model continuation

**Date:** 2026-09-08
**Status:** accepted

## Context

F08 recovery previously passed the journal straight to the model. A crash after
an assistant tool request but before its result left an invalid provider history.
Scripted test models accepted this shape, so successful local resume did not prove
that the OpenAI/DeepSeek adapters could continue it. Asking the model to reissue
the call also allowed it to change the arguments or call id before recovery.

F10 additionally needs to distinguish a new intent-protected turn from legacy
execution: before the intent protocol existed, absence of an intent did not prove
that an external operation had not started.

## Decision

An explicitly authorized durable resume first restores the unfinished **final**
tool batch. It uses its original ids and frozen arguments, does not append the
assistant request twice, and does not re-run calls with journalled results. Only
after every call has a result does the model get another request. Replaying a
batch does not consume an additional model-call iteration. Recovery is sequential;
the ordinary fresh-call path retains independent read-only parallel execution.

Result restoration uses the turn's tool cache or a recorded effect matched by
turn, call id, tool and exact arguments. A known legacy idempotency key can supply
a recorded result, but a missing row cannot grant replay permission. Recorded
results bypass a new approval prompt because no new operation is dispatched.
Recovered external results retain untrusted framing; results of removed tools
are framed conservatively. Unknown tools may have recoverable results even when
their implementation is no longer registered.

New calls can execute only through the existing approval/intent boundary and the
tool's replay classification. Unrecorded delegates/custom pipelines are not
blindly restarted: their child-level execution evidence is not yet durable enough.
Their completed cached results can still be restored. This is an explicit pending
F08/F10 integration requirement, not a substitute for eventual pipeline recovery.

Migration v004 adds `active_turns.effect_protocol`, default zero for existing rows.
Only `begin_turn` in the intent-aware runtime writes version one. The effect
reservation API rejects new dispatch for legacy versions, while preserving access
to recorded results. Read-only legacy operations remain available. No old row is
automatically promoted based on elapsed time, a restart or absence of intents.

Journal decoding and tool pairing are strict. Corrupt rows, duplicate/orphan
results, ambiguous non-final batches, non-JSON arguments and reuse of a call id
with changed arguments stop continuation rather than silently becoming a success.
The original journal is retained. Report-only recovery closes missing result
slots in *conversation history* with an explicit unverified-interruption marker,
not a fabricated success/failure. It does not insert that marker into the tool
cache or execution journal. Corrupt journals do not overwrite previous history.

Completed history trimming omits the orphan result tail of a discarded old batch
instead of starting a provider request with ToolMessages lacking their request.
Legacy leading orphan results are omitted on read without rewriting their source.
This does not sanitize or truncate active execution evidence: malformed active or
middle-of-history batches still require review.

## Alternatives

### Let the model regenerate the unfinished request

Rejected: the incoming history can be invalid before the model answers, and a
regenerated call may represent a changed operation rather than the same intent.

### Fill every missing result with a synthetic error and continue execution

Rejected for active resume: it erases the distinction between not started,
completed and unknown outcomes, potentially encouraging a duplicate operation.
An explicit unknown marker is used only in report-only history, never as evidence
authorizing a retry or as a successful tool-cache entry.

### Replay the whole turn or every delegation

Rejected: completed sibling operations and unjournalled custom pipeline effects
could repeat. Recorded parent results are reusable; partial child execution needs
its own durable continuation/reconciliation contract.

## Consequences

### Positive

- Adapter requests contain complete call/result pairs after recovery.
- The original operation identity survives the crash rather than being guessed
  by a new model call; journal/cache commit failures stop further work.
- Legacy data is not silently treated as intent-protected execution.
- Known results can survive a removed tool or changed key implementation.

### Negative

- Legacy unrecorded writes and partial unclassified/delegated pipelines require
  explicit reconciliation rather than automatic replay.
- Corrupt journals fail closed. Historical reconciliation and a complete operator
  workflow remain required; a failed turn alone is not a resolved business action.

### Neutral

- F11 ownership remains the caller's responsibility. This is not a distributed
  lock, provider-side exactly-once guarantee or durable delivery outbox.
- F08 plan-step lifecycle, F09 delivery and F10 global business-key retention,
  distinct operation ids and direct writer inventory remain open.
- Rollout requires the migration and coordinated replacement of old executors;
  no production changes are authorized by this local implementation.

## References

- [External effect intents](ADR-0007-external-effect-intents.md)
- [Conversation ownership](ADR-0008-conversation-execution-ownership.md)
- [Remediation tracker](../reviews/2026-09-08-remediation.md)
- `tests/test_durable_tool_replay.py`: independent strict model checks and actual
  ChatOpenAI/ChatDeepSeek adapters with an intercepted HTTP transport.
- `tests/test_durable_kill.py`: real SIGKILL before dispatch, before cache commit
  and before effect-result commit, plus exclusive-ownership crash scenarios.
