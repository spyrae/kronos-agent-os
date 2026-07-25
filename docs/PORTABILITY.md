# Kronos Agent OS (KAOS) — Portability

An agent is more than its code: it is a persona, a set of skills, and a memory
of you. A `.kaos` bundle makes that portable — you can move an agent between
machines, keep an offline backup, or bring a history that started somewhere else.

```bash
kaos export --out my-agent.kaos          # take your agent with you
kaos import my-agent.kaos --dry-run      # see what would change
kaos import-from auto ~/chatgpt-export   # bring history from another tool
```

## Bundle Format

A bundle is a zip archive. `manifest.json` lists every artifact with its
SHA-256, so an importer can prove the payload is exactly what the exporter
wrote before touching a database.

```text
manifest.json                    schema_version, kaos_version, agent_name,
                                 created_at, includes, counts, artifacts{path: sha256}
persona/{IDENTITY,SOUL,AGENTS,methodology}.md
skills/<name>/SKILL.md
skills/<name>/references/*
memory/facts.jsonl               {user_id, content, source, created_at, relevance?}
memory/graph.entities.jsonl      {name, type, properties}
memory/graph.relations.jsonl     {source{name,type}, target{name,type}, relation_type, properties}
memory/shared_facts.jsonl        this agent's contributions to the swarm ledger
schedule/tasks.jsonl             pending reminders and follow-ups
notes/**                         opt-in: --include-notes
sessions/sessions.jsonl          opt-in: --include-sessions (one row per thread)
```

Exports are **deterministic**: JSONL rows are sorted, the archive uses a fixed
entry timestamp, and `created_at` in the manifest is the only field carrying
wall-clock time. Exporting unchanged state twice produces a byte-identical
archive — which makes "did my agent's state actually change?" a checkable
question.

## What Is Never Exported

Enforced in code and covered by a negative test across every flag combination:

- `.env*` and any credential file
- Telegram `*.session` files
- the SQLite databases themselves (content is dumped as JSONL instead)
- the Qdrant vector store — vectors are re-embedded on import from the facts
- audit logs (`logs/*.jsonl`)
- other agents' shared facts, and skills from a shared workspace

Credential-looking strings inside exported content (bearer tokens, `sk-…` keys,
`api_key=` parameters) are redacted. Two levels apply:

| Content | Treatment | Why |
|---------|-----------|-----|
| Persona, notes, facts, graph, schedule | credentials stripped, PII kept | This is your own material — masking your own email out of your own facts would destroy what the bundle carries. |
| Sessions and tool output | credentials stripped **and** PII masked | Third-party data you did not author. |

Transport identifiers (`chat_id`, `topic_id`, `thread_id`) identify someone's
private chats, so they are dropped by default and the task is marked
`needs_rebind`. `--include-transport-ids` keeps them; the CLI warns when you do.

## Importing

```bash
kaos import my-agent.kaos [--merge skip|overwrite|append] [--dry-run] [--rebind-chat <id>]
```

Verification runs first: a single altered byte aborts the import with nothing
written. Then each section is merged by a content key, never by id — ids from
another installation mean nothing locally, which is also what makes a second
import a no-op.

| Section | Dedupe key | Merge behaviour |
|---------|-----------|-----------------|
| Facts | normalized text + user | duplicates skipped (whitespace/case-insensitive) |
| Graph entities | `(name, type)` | properties merged |
| Graph relations | `(source, target, type)` | insert-or-ignore |
| Skills | name | installed as **draft**; existing names kept unless `--merge overwrite` |
| Persona | filename | kept local on `skip`; `append` adds a marked section; `overwrite` replaces |
| Notes | relative path | same three modes |
| Schedule | — | skipped unless `--rebind-chat` says which chat they belong to |
| Sessions | — | archived into `notes/inbox/imported-sessions-<agent>.md` |

Two deliberate refusals:

- **Imported skills are drafts.** A skill is executable procedure; it gets
  reviewed before the agent relies on it.
- **Session history is never written into the live session store.** Thread ids
  come from another installation, and overwriting a real conversation to
  "restore" a foreign one is data loss wearing a feature's clothes. The history
  lands in the inbox as markdown, where the agent can still read it.

`--dry-run` runs the same code path and reports the full result without writing.

## Importing From Other Tools

```bash
kaos import-from <tool> <path> [--dry-run] [--limit N] [--convert-only --out b.kaos]
kaos import-from auto ~/Downloads/chatgpt-export     # detect the format
```

Importers convert a foreign export **into a bundle**, then that bundle goes
through the normal verified path. One code path, one set of merge rules.

| Tool | Input | Extracted |
|------|-------|-----------|
| `chatgpt` | `conversations.json`, its directory, or the export zip | conversation threads (kept branch only) + explicit memory statements ("remember that…", "запомни…") |
| `claude-projects` | project directory or `projects.json` | instructions → persona draft, `skills/<name>/` → skills, other docs → notes |
| `obsidian` | vault directory | notes, `[[wikilinks]]` → graph relations, `type: fact` notes and `facts:` frontmatter → facts |
| `telegram` | `result.json` | owner identity from `personal_information`; conversations **only** for chats named with `--chat` |
| `letta` | `.af` agent file | persona block → IDENTITY.md, system prompt → methodology.md, human block → facts, messages → session |

Notes on limits:

- ChatGPT and Telegram exports are read whole; above 256 MB the import is
  refused with advice rather than getting OOM-killed halfway.
- `--limit` caps conversations (ChatGPT), notes (Obsidian), documents (Claude),
  messages per chat (Telegram), or trailing messages (Letta). Whatever gets
  dropped is reported, never silently trimmed.
- Telegram imports nothing conversational without `--chat`: ingesting every
  private chat someone ever had is not a sane default.

## Round Trip Check

```bash
kaos export --out before.kaos
kaos import before.kaos --dry-run     # expect zero new records
```

A dry-run import of your own fresh export should report nothing new. If it
reports additions, the dedupe key and the export disagree — that is a bug worth
filing.
