# ADR-0022: Unknown outcomes are closed by an operator, and deliveries are repaired, not faked

**Date:** 2026-10-06
**Status:** accepted

## Context

ADR-0007 made an external effect leave a durable intent before dispatch, and
ADR-0012/0013 moved plan and recovery notifications into a transactional
outbox. Four gaps remained:

1. A pending intent blocked its turn and the same operation everywhere, and no
   command could ever release it. The CLI only displayed it.
2. A delivery in `needs_review` stayed there forever and blocked every later
   message of its stream.
3. An ordinary answer was sent by the bridge after its turn was finalized. A
   crash in between lost it. Cron notifications that hit a 429/5xx were dropped.
4. Whether a tool call is an effect at all was decided by name patterns.

## Decision

**Effect reconciliation.** `kaos turns effects` lists unresolved intents;
`kaos turns reconcile <key> --outcome happened|did-not-happen|unknown` records a
verdict. The command must hold the conversation (ADR-0008), which is the proof
that no cooperative executor is still running it. `happened` turns the stated
result into the recorded effect, so a continuation reuses it. `did-not-happen`
releases the intent. `unknown` releases it and fails a still-active turn. Every
verdict is stored with the full original intent in `effect_reconciliations`
(v010). A turn that already failed is never revived: the user was told it had
stopped.

**Delivery repair.** `kaos delivery review` lists stuck deliveries as metadata.
`resolve --action retry` re-queues with the stored message identity;
`--rebind-sender` is the explicit opt-in to let another account take it over.
`--action abandon` marks it obsolete so the stream moves on. There is no
"mark delivered": no receipt exists and none is invented.

**Ordinary replies.** A turn whose answer the bridge sends itself is opened as
`reply owed` (v011) with its frozen route. The bridge confirms the send. At
process start, before the bridge handles anything, finished turns still owed
within 24 hours are handed to the existing outbox with their stored text.

**Cron notifications.** A send Telegram verifiably did not accept (429, 5xx, no
connection) is queued and retried with backoff, at most six times in 24 hours,
then marked dead and reported once. The queue stores the body, never the URL.

**Effect classification.** Each tool module declares its tools with
`mark_side_effect` or `mark_read_only`. An inventory test fails on an
undeclared built-in tool, on drift from the reviewed list, and on a new tool
group in `_init_tools`. Generated tools are effects. Composio actions get the
MCP policy without their app prefix.

## Alternatives rejected

- **Expire a pending intent after a timeout.** Time passing proves nothing about
  what the provider did.
- **Revive a failed turn after `happened`.** Its failure was already delivered;
  replaying would contradict what the user was told.
- **Send every ordinary reply through the outbox.** The reply path carries
  voice, buttons, reply threading, arbitration and the dissent gate; replacing
  it cannot be verified without live Telegram. Confirm-or-adopt adds recovery
  without changing how a healthy reply is sent.
- **Adopt unconfirmed replies periodically.** A live process may be mid-send;
  only at start is it certain that it is not.
- **Retry a timed-out notification.** The request may have arrived; a retry
  could post the digest twice.

## Consequences

- An answer sent just before the process died arrives again after restart,
  marked as a recovery message. Repeating is the lesser failure than losing it.
- If a reply path forgets to confirm, the next restart re-sends up to 24 hours
  of its answers. The main handler and the approval callback confirm; the
  text `/approve` path belongs to plans and recovery, which own their delivery.
- Notification retry is retry after a failed send, not a transactional outbox:
  a crash between a job producing its text and the first send still loses it.
- `effect_reconciliations` and dead notification rows are not pruned.
- Writers outside the engine are still not covered by the intent ledger:
  the knowledge pipeline queue, cron jobs that write workspace files
  (`persona_evolve`, `self_improve`, `skill_create`, `skill_improve`,
  `user_model`), the expense processor (which has its own per-item ledger),
  observer state, and the SEO/GEO and signal fetchers. They run on a schedule,
  not inside a durable turn, so a resume cannot replay them; their own
  idempotency has not been audited here.
- None of this was exercised against live Telegram, MCP servers or providers.
