# Kronos Agent OS (KAOS) — Memory System

KAOS memory is local-first and per-agent by default. The goal is to make an
agent durable without turning every conversation into unbounded prompt history.

## Memory Types

| Type | Store | Purpose |
|------|-------|---------|
| Session history | `data/<agent>/session.db` | recent conversation continuity per thread |
| Extracted facts | `data/<agent>/memory_fts.db` | exact keyword recall for names, IDs, dates, URLs, decisions |
| Vector memories | `data/<agent>/qdrant/` | optional semantic recall through Mem0/Qdrant local mode |
| Knowledge graph | `data/<agent>/knowledge_graph.db` | entities and relations extracted from durable facts |
| Shared coordination facts | `data/swarm.db` | cross-agent search and coordination when swarm mode is enabled |

Live memory files are runtime state. Keep `data/` gitignored.

## Session Memory

`kronos/session.py` stores LangChain messages as JSON in SQLite:

```text
thread_id -> [HumanMessage, AIMessage, ToolMessage, ...]
```

Thread IDs are transport-specific:

| Source | Thread ID |
|--------|-----------|
| CLI | `cli-test` by default |
| Telegram DM | `{chat_id}` |
| Telegram topic | `{chat_id}:{topic_id}` |
| Discord channel/thread | Discord IDs |
| Cron/webhook | configured job/session ID |

Only the recent window is kept in the session table. Transient system context
from group routing or peer reactions is not persisted.

## Context Budget

History is compacted by **size**, not by number of messages. The unit matters:
five turns each carrying a pasted document are ~57,000 tokens in ten messages,
while thirty-two one-line exchanges are under a thousand. A rule counting
messages gets both wrong — it ignores the first and summarises the second at the
cost of a model call.

| Trigger | When |
|---|---|
| Over budget | history exceeds `budgets.context_tokens` (default 12,000) |
| Long enough to be worth it | more messages than the strategy's window **and** at least a quarter of the budget |

The budget is the *persisted history*, not the model's context window: the system
prompt, retrieved memories and a turn's tool results are added on top of it.
Configure with `CONTEXT_TOKEN_BUDGET` or `budgets.context_tokens`; `0` means the
default.

Whatever the strategy, what survives is trimmed to the budget — a window of
twenty messages says nothing about the size of those twenty. One message larger
than the whole budget is still sent, because dropping it would lose the request
itself.

Compaction does not depend on long-term memory being configured. Keeping a
conversation sendable is context management; it used to be skipped wherever Mem0
was absent, so the deployments with no memory were also the ones carrying
unbounded histories.

### Tool output

The same question applies to what a tool returns:

| Tool | Seen by the model |
|---|---|
| Verbose by nature (search, fetch, dumps) | first 2,400 characters, or a summary of the first 8 records |
| Everything else | up to 8,000 characters |
| Declares `output_max_chars=0` | all of it |

The last row is for output that is bounded by construction and whose value is
being complete — a ranking cut in half is a different ranking, and half a skill
reads as a whole procedure. The full text is always kept in the tool-call audit
regardless of what the model was shown.

## Long-Term Recall

When memory is enabled and `DEEPSEEK_API_KEY` is available, KAOS can extract
facts from completed turns in the background. Runtime failures in memory writes
are logged but should not crash the primary chat path.

Recall before an LLM call can combine:

1. FTS5 keyword matches for exact facts.
2. Mem0/Qdrant semantic results when the optional memory extra is installed.
3. Knowledge graph context for known entities and relations.

The runtime injects only the relevant recall results into the working context,
not the entire memory database.

## Knowledge Graph

`kronos/memory/knowledge_graph.py` stores:

- entities such as `person`, `company`, `project`, `concept`, `tool`, `location`, `event`
- relations such as `knows`, `works_at`, `uses`, `owns`, `related_to`, `part_of`, `created`

The graph is local SQLite and is useful for relationship-heavy recall, for
example "what projects did we connect to this vendor?"

## Privacy And Deletion

Memory may contain private user facts, tool outputs, paths, IDs, and decisions.

Dashboard inspection:

- `/api/memory/records` lists FTS facts, shared facts, knowledge graph entities/relations, and session rows with type/source/session/template metadata.
- The Memory Inspector supports query/type/source/session filtering, record details, recall rationale, per-record delete, and scoped reset.

Deletion controls:

- `/clear` or `/reset` clears the current transport thread.
- `SessionStore.clear(thread_id)` deletes the matching session row and legacy checkpoint rows.
- `DELETE /api/memory/records/{id}` deletes an inspectable record such as `fts:12`, `shared:7`, `entity:3`, `relation:9`, or `session:<thread>`.
- `POST /api/memory/reset` with `confirm=true` resets a scoped store: `facts`, `shared`, `knowledge_graph`, `sessions`, or `all`.
- Operators can remove per-agent runtime state by deleting `data/<agent>/`.
- Operators can remove all cross-agent coordination state by deleting `data/swarm.db`.

Do not commit memory databases, Qdrant directories, Telegram sessions, live
workspace files, or audit logs.

## Retention

Current retention behavior is intentionally conservative:

- Session history is capped by `MAX_HISTORY` in `kronos/session.py`.
- FTS facts include timestamps, relevance, and last-access metadata.
- `kronos/cron/sleep_compute.py` can consolidate facts and prune stale entries.
- `kronos/cron/swarm_retention.py` prunes old group coordination messages from `swarm.db`.

Retention jobs should be visible in logs and safe to run repeatedly.

## Example Recall

User says:

```text
Remember that launch reviewers prefer concise technical answers.
```

Later:

```text
Draft the launch note for reviewers.
```

KAOS can retrieve the durable preference and inject a compact memory context:

```text
[Relevant memories]
- Launch reviewers prefer concise technical answers.
```

The model sees the relevant fact without seeing unrelated prior conversation.

## Operational Notes

- `kaos chat --no-memory` disables long-term memory for local debugging.
- Missing memory dependencies should degrade gracefully.
- Memory extraction should not store peer-reaction metadata from swarm/group flows.
- Public examples should use synthetic memory data only.
