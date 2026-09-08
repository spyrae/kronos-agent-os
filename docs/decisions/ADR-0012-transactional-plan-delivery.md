# 0012: Transactional plan delivery

**Date:** 2026-09-08
**Status:** accepted

## Context

A plan summary used to be stored before `send_webhook`, whose boolean result was
ignored. Once stored, the summary was no longer pending, so an unavailable bridge
lost the owner's notification forever. Progress had the same commit-to-send gap.
ADR-0011 deliberately did not equate a stop request with safe cleanup or delivery.

Plan state and session/turn state live in different SQLite databases. A separate
queue database would introduce another two-database commit gap. The delivery
contract must not re-execute a plan or regenerate its model answer after a send
failure, and must survive process death after remote acceptance but before a
local acknowledgement.

## Decision

1. Introduce v007 `delivery_outbox` in the producer's own SQLite database. Its
   insert runs inside the same transaction as the proven step outcome or frozen
   summary. A failed insert rolls back the producer update; an existing identity
   with different content/destination is an error. The schema can be reused by
   other producers, but this step connects **plans only**.
2. Persist immutable plain-text chunks, chat/topic, per-chunk Telegram `random_id`,
   sender binding, retry metadata, progress and correlated server message IDs.
   Redact before enqueue and split at Unicode boundaries below the UTF-16 limit.
   Each completed plan turn has a distinct progress identity, including re-parks.
   The summary is first-writer-wins; delivery retries never call the model.
3. Use `pending`, `delivered`, and `needs_review` separately from execution states.
   Delivered means that Telegram accepted all chunks, **not** that the owner read
   them. A false/empty/uncorrelated acknowledgement does not advance the cursor.
   Persist a confirmed chunk before moving to the next one.
4. Hold a non-expiring kernel ownership lock, scoped by producer database and
   event identity, through dispatch, receipt and failure metadata commits. A
   second worker skips a live owner. Process death releases the lock without
   deleting or replacing its sidecar. Cancellation does not fabricate a receipt.
5. An independent application service drains bounded work every five seconds;
   the plan poller only enqueues and never awaits transport. No sender identity means no
   attempt. Retry backoff is capped at an hour but honors longer FloodWait delays.
   Each send has a 30-second cancellation timeout. A noncooperative async sender
   can exceed it while cancellation unwinds; ownership stays held until it exits.
   A failed stream does not block unrelated plans, while later notifications of
   the same plan wait behind its unacknowledged predecessor.
6. Send via the already authenticated Telethon client using MTProto
   `messages.sendMessage`. Always reuse the stored `random_id`, destination and
   text. Do not fall back to Bot API/new message identity, enable paid-message
   flags, or infer a correlated ID from an unrelated update. Pin the sender
   before dispatch; an account change or `RANDOM_ID_DUPLICATE` without a receipt
   requires review, not a new random ID.
7. Newly requested cancellations owe a final deterministic stop notice after
   fenced cleanup, including review-required outcomes. Historical cancelled
   plans are not automatically replayed as notifications on upgrade (`stop_notify`
   defaults to zero). Legacy nonempty summaries have unknown delivery; never
   backfill them as delivered or blindly resend old messages.
8. API, Dashboard, CLI and plan tools expose delivery separately, including
   queued/review counts and the legacy-unknown distinction. Logs contain internal
   sequence/error classifiers, not recipient, payload or exception strings.

## Alternatives

### Move `set_summary` after a successful HTTP call

Rejected: a crash after the actual send repeats model generation and can send a
different answer. Progress still has a producer/send crash window. An HTTP 200
alone may not identify which Telegram message was accepted.

### Shared queue in a new database or a separate broker

Rejected for this step: a new dependency or non-atomic cross-database enqueue
would need another recovery protocol. Co-locating the duty with its producer
gives a straightforward transactional invariant without installation/config work.

### In-memory retries, expiring worker leases, or replacement random IDs

Rejected: restarts lose in-memory duty; a lease can expire while a sender is
still alive; replacing the message identity after uncertainty can duplicate the
notification. Ownership and durable identity address different failure modes.

## Consequences

### Positive

- Result/notification gaps close for new plan progress and summaries.
- Slow/unavailable Telegram does not require another execution or model answer.
- Partial sends restart from the first unacknowledged chunk with the same ID.
- Cancellation notices are truthful and wait for cooperative stop reconciliation.

### Negative

- Plain text deliberately omits Markdown formatting and disables link previews.
- A review-required predecessor blocks later notices in that plan until operator
  reconciliation is implemented; the UI makes this visible rather than silently
  claiming delivery. Generic operator reconciliation remains required work.
- Payload/key retention is not implemented in this step. Durable duties and
  tombstones must not be deleted in a way that re-creates deliveries; scoped reset
  and retention need coordination with producers (F13 and remaining F09/F10).

### Neutral

- The local fake provider proves replay uses the same identity, not unlimited
  deduplication guarantees of the real Telegram server. MTProto specifies random
  IDs for resend prevention; this is not an unconditional exactly-once or human
  read guarantee. Live userbot/bot/topic acceptance remains a rollout gate.
- Generic resumed answers, approval prompts/continuations, normal bridge replies
  and other cron notification producers still require explicit durable wiring.
  In particular, startup auto-resume still precedes bridge readiness: this change
  does not claim to fix that separate session-result delivery gap.
- No production rollout or state migration was executed by this implementation.
  A mixed release must not use the old direct-send poller against new producers.

## References

- [Telegram messages.sendMessage](https://core.telegram.org/method/messages.sendMessage)
- [Telegram updateMessageID mapping](https://core.telegram.org/api/updates)
- [0011: Cooperative plan stop](ADR-0011-cooperative-plan-stop.md)
- [0008: Conversation ownership](ADR-0008-conversation-execution-ownership.md)
- `tests/test_delivery.py`, `tests/test_telegram_delivery.py`
- `tests/test_delivery_kill.py`: producer rollback, queued commit, accepted send,
  partial acknowledgement and final acknowledgement SIGKILL/restart scenarios.
- `docs/reviews/2026-09-08-remediation.md`: complete scope and remaining gates.
