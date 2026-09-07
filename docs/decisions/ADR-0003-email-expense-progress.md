# ADR-0003 — Per-item progress for email expenses

- **Status:** accepted
- **Date:** 2026-09-07
- **Context:** review F05

## Context

One email can contain several expenses. An email-level success flag consumed
failed siblings; retrying the whole email instead would repeat successful POSTs.
The canonical Notion writer stores a Ref but does not enforce uniqueness on it.

## Decision

Persist the validated extraction once, before writes, in `email_expense_items`.
The key is `(message_id, item_index)` within that frozen snapshot. Preserve the
source and fallback transaction date. Single-item messages retain the old Ref;
multi-item messages use `message_id:ordinal`. Raw email bodies are not copied.

Each item is claimed atomically. Known successes are not invoked again after a
restart; definite failures retry without re-extraction. Pending records are linked
atomically to their item. Manual resolution/discard updates only that item, then
derives email completion from *all* items and legacy pending siblings. Only a
complete recorded/duplicate email may be archived. Recovery also finds fully
completed snapshots whose email summary was not committed before a crash.

A missing/ambiguous POST response is not proof of failure. Raised writer errors,
canonical Notion transport errors, unexpected responses, and interrupted in-flight
claims remain non-terminal and require reconciliation. They are reported rather
than replayed automatically. Manual resolutions use an atomic claim as well.

## Alternatives

- Move the email error branch before success: fixes premature completion but
  repeats already successful writes; rejected.
- Re-extract and deduplicate by amount/date: loses distinct equal-value charges
  and depends on stable model output; rejected for within-email identity.
- Use a distributed transaction across SQLite/Notion/BUDGET.md: those systems do
  not share a transaction boundary. Do not claim exactly-once delivery.

## Migration and compatibility

An additive, idempotent Python migration creates the item table and version
marker. Python packaging already includes the package; no dependency or config
changes are required. Existing processed/pending rows are retained. Previously
misclassified historical emails are **not** automatically reopened: their external
writes must be reconciled first to avoid duplicate expenses.

## Consequences and boundaries

The existing cross-message amount/date dedup policy is preserved; every recorded
item now participates, not just the first item in an email. Equal-value sibling
items are not mistaken for cross-message duplicates.

This closes ordinary partial-result loss/replay, not all external-effect risks in
F10. An operator must verify Notion and the budget before resolving an uncertain
item. There is no automatic expiry/replay of unknown writes. Archive delivery
retries and the independent transaction gap between Notion and the budget are
separate concerns. A failed pre-write audit can retry safely; an unexpected crash
while claimed conservatively requires reconciliation even if no POST was sent.

## Verification

Synthetic tests cover mixed recorded/error/pending outcomes, all currencies,
restart with changed extraction inputs, manual sibling resolution/discard,
concurrent claims, uncertain writes, dry run, migration preservation, and recovery
between item completion and email finalization. No Gmail/Notion production calls.
