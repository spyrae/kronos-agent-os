# Kronos Observer / Capture Engine

Observer is the local-first layer that notices personal context without acting
as an autonomous outbound assistant. Its first job is to prevent useful context
from being lost while keeping privacy boundaries explicit.

## Contract

Observer may:

- save explicit captures from the user: voice notes, text notes, links, OSINT
  requests, and document captures;
- read dialog metadata/snapshots for configured digest jobs;
- keep cursors, ignored/muted peers, and digest timestamps under
  `workspace/ops/observer/`;
- store summaries, short excerpts, and structured metadata needed for recall.

Observer must not:

- call Telegram read acknowledgements from scan/digest flows;
- persist full personal chat transcripts from background scans;
- send messages to people or groups without an allowlist plus schedule or an
  explicit user command;
- require Raindrop or any remote bookmark provider for local capture;
- write raw PII into logs, run metadata, or observability traces.

## Source kinds

- `telegram_voice_note`
- `telegram_link`
- `telegram_text_capture`
- `telegram_unread_digest`
- `telegram_reply_debt`
- `telegram_daily_scope`
- `observer_manual_command`
- `osint_person`
- `document_capture`

Raw content is allowed only for explicit captures:

- `telegram_voice_note`
- `telegram_link`
- `telegram_text_capture`
- `osint_person`
- `document_capture`

Digest and scope flows should persist summaries/excerpts, never full transcripts.

## Local state

Observer state lives outside durable agent session history:

```text
workspace/ops/observer/state.json
workspace/ops/observer/runs.jsonl
workspace/notes/user/daily-scope/YYYY-MM-DD.md
workspace/notes/world/contacts/{slug}.md
```

`state.json` stores only control data:

- per-dialog cursors;
- last seen message ids;
- ignored peers;
- muted peers;
- sanitized ignore/mute reasons;
- last digest timestamps.

`runs.jsonl` is append-only run metadata. It is sanitized before writing and is
for operational debugging, not for storing message bodies.

## Implementation guardrails

- Keep the model/state layer side-effect-free: no network calls, no Telegram
  client imports, no LLM calls.
- Use `kronos.observer.models.ObserverSourceKind` for stable source labels.
- Use `kronos.observer.state.ObserverStateStore` for idempotent state
  persistence.
- Future scanner code must prove it does not call `send_read_acknowledge`.

## Manual controls

Observer v1 controls are available through allowed Telegram DMs only. Group
commands are ignored.

```text
/observer status
/observer ignore <peer> [reason]
/observer unignore <peer>
/observer mute <peer> [reason]
/observer unmute <peer>
/observer debts
/observer digest dry-run
```

Notes:

- `<peer>` is the peer id/token shown in status or scanner-derived summaries.
- `ignore` and `mute` both exclude peers from scanner snapshots and reply-debt
  detection. `ignore` is for privacy/no-scan use cases; `mute` is for noisy
  peers that should not trigger reminders.
- `status` reports job enablement, last runs, and ignored/muted peer ids. It
  intentionally does not include raw chat messages or run metadata.
- `digest dry-run` builds the morning digest without sending a scheduled
  notification and without updating scanner cursors.
- Manual commands append sanitized audit records to
  `workspace/ops/observer/runs.jsonl`.
