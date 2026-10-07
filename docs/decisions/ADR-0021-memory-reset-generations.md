# ADR-0021: A memory reset is ordered against writers by a generation

**Date:** 2026-10-06
**Status:** accepted

## Context

Two resets existed and neither did what it said. `/clear` deleted a thread's
stored history but left its messages in session search, and a turn still
running in that thread saved the history it had loaded straight back. The
dashboard reset deleted FTS rows while the same facts stayed in Mem0, did not
touch session search, and reported `ok` whatever happened; a turn finishing
afterwards re-extracted its facts into every layer.

Memory is written after the response, in an executor thread, by work that
started before any reset the operator issues meanwhile.

## Requirements

- A reset names the layers it covers and fails if one of them fails.
- Data removed by a reset does not come back from work that predates it.
- Other users keep writing during one user's reset.
- No new dependency, and no lock that expires by timer.

## Decision

**Conversation clear** takes the conversation ownership introduced in ADR-0008.
It therefore runs after a live turn rather than under it. Parked and interrupted
turns rebuild history from storage when they continue, so they have nothing old
to write back. `SessionStore.clear` also removes the thread from session search.

**Memory reset** (`kronos.memory.reset`) advances a generation in `swarm.db`
(`memory_generations`, per user or global) before deleting anything. A writer
captures the generation when its work starts:

- Per-agent stores (Mem0, FTS, knowledge graph) are written under an in-process
  gate shared by writers and held exclusively by a reset. A writer entering
  after the reset sees a changed generation and stands down; a writer already
  inside finishes, and the reset then deletes what it wrote.
- Shared facts check the generation inside the inserting transaction, which
  also orders writers in other agent processes.
- If the generation cannot be read when a turn starts, that turn is not stored.

Sessions are reset thread by thread under ownership, then the remaining index
rows of this agent are removed. Deleting a single fact removes its Mem0 and
shared copies as well, Mem0 first, so a failure leaves the fact listed.

## Alternatives rejected

- **Timestamps instead of a generation.** Comparing a reset time with a turn's
  start time depends on clock resolution and on every writer recording a start.
- **One mutex for all memory writes.** Extraction takes seconds per turn; it
  would serialize every user's storage behind the slowest one.
- **Cancel running turns on reset.** A started external request is not undone
  by cancellation (ADR-0011), and the turn would still need ordering.
- **A flag on in-flight turns that trims history at finalize.** Unnecessary once
  clear is ordered by ownership, and it would need a schema change.

## Consequences

- `/clear` and a session reset wait for a turn that is running in that thread.
- A reset on one agent covers that agent's private stores and the shared ones.
  Other agents' Mem0, FTS, graph and histories are reset from those agents.
- Durable turn outcomes and delivery records are execution evidence; they stay
  under their own retention and are not memory in this sense.
- Mem0 `delete`, `delete_all` and `reset` are exercised against the real
  mem0ai 1.0.7 package where it is installed, and against a fake elsewhere.
  Listing uses an explicit high limit because Mem0 pages at 100.
- Facts imported by an operator bypass the generation: an import is a new,
  deliberate write.
- The dashboard has one principal, the operator, who manages every user's
  memory by design; `user_id` on the reset endpoint narrows a reset, it is not
  an access check. If the dashboard ever gains per-user accounts, the memory
  endpoints need authorization per principal.
