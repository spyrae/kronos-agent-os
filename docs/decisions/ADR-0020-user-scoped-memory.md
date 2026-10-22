# ADR-0020: Every durable memory row has one owner

**Date:** 2026-10-06
**Status:** accepted

## Context

`session_search` and `SwarmStore.search_sessions` matched every indexed message
of every agent. The knowledge graph had no user column, so `retrieve_memories`
injected any matching entity into any caller's prompt, and the nightly sleep
compute linked all users' facts into one graph. Anyone who could reach an agent
(an open allowlist, a group member, a Discord user) could read another user's
conversations and graph.

No trusted identity was in reach of a tool. The only user id available was the
audit context, which on an approval continuation is the approver and on durable
resume is absent.

## Requirements

- The owner comes from the transport-verified sender, never from model
  arguments, and survives approval and restart.
- An empty or unknown owner reads nothing and cannot write.
- Rows whose owner was never recorded are not given to the first requester.
- No data is deleted or merged by the migration.

## Decision

`active_turns.user_id` (migration v009) records the sender at `begin_turn`.
`kronos.memory.scope` binds that owner in a ContextVar for a fresh turn, for an
approval continuation (from the turn, not from `decided_by`) and for durable
resume. Tools read it with `current_data_owner()`.

`session_messages.user_id` and `entities/relations.user_id` are nullable. New
rows are written under the turn owner; `NULL` means "owner unknown". Search and
graph reads take a required `user_id` and filter on equality, so `NULL` rows and
an empty owner match nothing. An already indexed session row keeps its first
owner. Entity names are unique per owner.

Sleep compute runs one extraction per user. The user-model cron searches per
configured owner and ignores audit entries of non-owners, because
`USER-MODEL.md` is injected into every prompt. Bundles carry the owner through
export and import; ownerless bundle rows land under the agent name, as facts
already do.

Ownerless rows are assigned only by an operator: `kaos sessions owners` lists
counts, `kaos sessions assign-owner --user-id … [--thread …] [--graph]` assigns.
A graph entity whose name the user already has stays ownerless.

## Alternatives rejected

- **Derive the owner of legacy rows from the thread id.** A Telegram DM thread
  id equals the peer id, but group, Discord, plan and CLI threads do not follow
  that rule, and a wrong guess publishes private history. Left to the operator.
- **Reuse the audit context as the identity.** Its `user_id` is deliberately the
  approver on approval; changing that would falsify the audit trail.
- **Scope search by thread instead of user.** The tool exists to search across
  a user's conversations with all agents; thread scope removes that purpose.

## Consequences

- After upgrade, existing session-search rows and the existing graph are
  invisible to users until the operator assigns them. Session search heals by
  itself within its 30-day window; the graph does not.
- In a group thread a row belongs to the user whose turn first indexed it. The
  same text already indexed in a conversation is never indexed again, so
  trimming history cannot hand an earlier participant's messages, or ownerless
  legacy rows, to whoever spoke last.
- Rolling back to code from before this decision: the added columns and
  tables are ignored by older code. The one exception is the knowledge graph:
  older code re-creates `UNIQUE (name, type)` on `entities`, which fails while
  two owners have an entity with the same name and type. Check that before
  rolling back; the graph is then unavailable, the agent itself keeps running.
- With an open allowlist and no configured owner, the user-model cron still
  reads all audit entries; only the session context is withheld.
- Dashboard memory views and the sleep-compute digest remain operator views
  across owners.
- Telegram and Discord ids share one namespace until A01 separates them.
- Scoped reset across all layers is F13 and is not part of this decision.
