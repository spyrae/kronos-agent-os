# ADR-0004 — Serialize budget read/modify/write transactions

- **Status:** accepted
- **Date:** 2026-09-07
- **Context:** review F06

## Context

Atomic file replacement prevents partial reads but does not protect a read/modify/
write sequence. Concurrent expenses or tranche edits could read the same balance
and overwrite each other's deductions. Agents run in separate processes.

## Decision

The canonical IDR expense writer and both tranche editors hold a process/thread
lock for the entire operation. A stable `BUDGET.md.lock` sidecar uses POSIX flock;
it is never removed or atomically replaced. Locking BUDGET.md itself would leave
concurrent writers locking different inodes after replacement.

Resolve file aliases before both locking and writing, so symlink paths refer to
one transaction boundary. Bound lock acquisition to 30 seconds and fail before
Notion writes when a lock cannot be acquired. Release locks/file handles on every
exit path. Tranche edits now use atomic replacement, like expense deductions.
RUB/USD operations do not change tranches and retain their lock-free atomic rate
snapshot reads; their availability does not depend on a budget lock/file.

## Alternatives

- Atomic replace alone: preserves whole files, but loses concurrent changes.
- A threading.Lock alone: cannot coordinate the six agent processes.
- Lock the budget file inode: replacement invalidates the shared lock identity.
- Optimistic retry after Notion POST: risks duplicate writes and amounts computed
  from outdated tranche rates; rejected.
- Move the budget into SQLite: possible future architecture, but not required for
  the minimal fix and would change the human-readable budget source of truth.

## Consequences and boundaries

Compatible with the project's macOS/Linux hosts, using only the standard library.
Mutations of the same budget serialize, including time spent in the existing
Notion writer; other callers can get a retryable busy response. Reads see a whole
old or new file. This is not a distributed transaction with Notion: a crash or
file-write failure after a successful POST still requires reconciliation (F10).
Advisory locks cannot protect against editors/scripts that ignore the protocol.
The sidecar must not be deleted while agents are running.

## Verification

Tests run real concurrent threads and two spawned processes, including different
symlink aliases of the same budget. They cover expense/expense, expense/tranche,
lock timeouts before any POST, lock release on failure, atomic-write failure
without truncation, and stable sidecar inode identity. Notion is mocked.
