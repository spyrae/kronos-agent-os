# SQL query construction review

**Reviewed:** 2026-07-13

This review covers every Bandit `B608` finding ("possible SQL injection through
string-based query construction") in production code. Each was inspected for
untrusted input reaching the query body. **None interpolates untrusted or
user-controlled data into SQL.** All bound values are passed as parameters
(`?` placeholders); the only interpolated fragments are fixed literals or
server-generated values. These are false positives that Bandit cannot rule out
statically, retained here as a documented review rather than suppressed inline.

## Patterns

1. **Dynamic `IN (...)` list** — the interpolated fragment is a run of `?`
   placeholders (`",".join("?" for _ in items)`); every actual value is bound
   as a parameter. Bandit sees an f-string touching SQL and flags it, but no
   data crosses into the statement text.
2. **Fixed condition fragments** — optional `WHERE` clauses are assembled from
   hard-coded string literals (e.g. `"AND agent_name = ?"`); values are always
   parameterized.
3. **Server-generated HogQL** — analytics queries against PostHog's HogQL API
   interpolate only a server-produced UTC timestamp and hard-coded event names.

## Findings

| Location | Pattern | Why it is safe |
| --- | --- | --- |
| `dashboard/api/swarm.py:125` | Dynamic `IN` | `msg_id IN ({placeholders})` where `placeholders` is `?`-only; message ids bound via params. |
| `dashboard/api/swarm.py:225` | Dynamic `IN` | `session_id IN ({placeholders})` where `placeholders` is `?`-only; council ids bound via params. |
| `kronos/analytics/sources/posthog.py:71` | HogQL timestamp | Only `{since}` (from `datetime.now(UTC).strftime(...)`) is interpolated; event name is a literal. |
| `kronos/analytics/sources/posthog.py:79` | HogQL timestamp | Same server-generated `{since}`; `auth_email_verification_completed` is a literal. |
| `kronos/analytics/sources/posthog.py:84` | HogQL timestamp | Same `{since}`; `trip_created` literal. |
| `kronos/analytics/sources/posthog.py:90` | HogQL timestamp | Same `{since}`; event names are literals. |
| `kronos/analytics/sources/posthog.py:95` | HogQL timestamp | Same `{since}`; `poi_saved` literal. |
| `kronos/analytics/sources/posthog.py:98` | HogQL timestamp | Same `{since}`; event names are literals. |
| `kronos/swarm_store.py:971` | Dynamic `IN` | `id IN ({placeholders})` where `placeholders` is `?`-only; ids are `int()`-coerced and bound via params. |
| `kronos/swarm_store.py:1089` | Fixed fragment | `LIKE ? {agent_filter}`; `agent_filter` is a literal (`"AND agent_name = ?"`) or empty; values bound via params. |
| `kronos/swarm_store.py:1159` | Fixed fragment | `WHERE {where}`; `where` joins hard-coded condition literals, each using `?`; values bound via params. |
| `kronos/swarm_store.py:1182` | Fixed fragment | `WHERE created_at > ? {agent_filter}`; `agent_filter` is a literal or empty; values bound via params. |

## Rule for new queries

Interpolating anything other than a `?`-placeholder run or a fixed literal
fragment into a SQL string is not allowed. User input, request fields, and
external data must always be bound as parameters. Table or column names that
must be dynamic require an explicit allow-list, never string interpolation of
caller-supplied values.
