# Education Reminders Design

Education Reminders is a proposed low-priority scheduled broadcaster for
allowlisted учебные группы. It is **not** part of Observer core: unlike
Capture/Daily Scope, this feature sends outbound messages to real students and
therefore requires stricter gates than read-only observer flows.

Decision for v1: **later / gated**. Keep only design and pure model contracts
until the owner explicitly opts into an outbound automation project.

## Desired UX

1. User creates a scheduled announcement with a fixed template.
2. Kronos renders a preview/dry-run.
3. User approves the preview and target group is allowlisted.
4. Cron evaluates schedule and safety gates.
5. Message is sent at most once per idempotency key.
6. Audit log records preview/send/skip without private student data.

Example message template:

```text
Сегодня в {lesson_time} занятие. Домашку сдать до {deadline}.
```

## Model

Pure contracts live in `kronos/education_reminders/models.py`.

### ScheduledAnnouncement

- `target_group_id`: Telegram group id.
- `topic_id`: optional forum topic id.
- `message_template`: deterministic template, no LLM generation by default.
- `schedule`: cron/ISO-like schedule string owned by future scheduler adapter.
- `timezone`: IANA timezone, defaults to UTC.
- `idempotency_key`: stable sha256-derived key from group/topic/schedule/template.
- `enabled`: false by default.
- `preview_required`: true by default.
- `quiet_hours`: local time quiet window.

### AnnouncementPreview

Dry-run artifact shown to the user before first send. It contains rendered text,
target, schedule, timezone, topic id, and idempotency key.

### AnnouncementSendRecord

Append-only audit entry for preview/send/skip/block decisions:

- idempotency key;
- target group/topic;
- status: preview, sent, skipped, blocked;
- scheduled time;
- reason.

## Safety gates

A send is blocked unless all gates pass:

1. `dry_run == false`.
2. Announcement is enabled.
3. Target group id is in allowlist.
4. Preview is approved when `preview_required` is true.
5. Idempotency key has not been sent before.
6. Current local time is outside quiet hours.
7. Audit log can be appended.

If any gate fails, no outbound send happens. The scheduler should record a
blocked/skipped audit entry with reason, not message body.

## Idempotency

Idempotency key is derived from:

```text
target_group_id | topic_id | schedule | message_template
```

A future send service must persist sent keys under something like:

```text
workspace/ops/education-reminders/sent.jsonl
workspace/ops/education-reminders/audit.jsonl
```

Before sending, the service checks the sent-key set. Duplicate keys are blocked.
This prevents duplicate announcements after restarts or repeated cron ticks.

## Integration points

- Cron scheduler: a future job can evaluate configured announcements every
  minute or every 5 minutes.
- Sender: use `send_bot_api(text, chat_id=target_group_id, topic_id=topic_id)`
  only after gates pass.
- Bridge: optional management commands later, e.g. preview/approve/list. Not in
  RB-1287.
- Storage: local workspace JSONL config/audit only.

## What not to do in v1

- Do not send real messages from the design/model layer.
- Do not generate public student-facing text with an LLM unless preview is
  mandatory and approved.
- Do not auto-reply to students.
- Do not connect arbitrary groups; allowlist only.
- Do not bypass quiet hours or idempotency.

## Go / no-go

Recommendation: **no-go for Kronos core v1**. Keep this as later operational
automation because it is outbound and user-visible. It can proceed only after:

- owner provides explicit group allowlist;
- preview/approval UX exists;
- audit/sent-key store exists;
- dry-run has been validated in production-like environment.

## Implementation plan if revived

1. Config loader for allowlisted groups and announcement definitions.
2. Preview command: render template and persist pending approval.
3. Approval command: mark preview approved for idempotency key.
4. Cron evaluator: load announcements, evaluate gates, append audit.
5. Send adapter: call `send_bot_api` only after gates pass.
6. Quarantine mode: any uncertain config stays disabled.

## Acceptance checks

- Unit tests cover idempotency, quiet hours, preview requirement, allowlist, and
  dry-run gate.
- No outbound side effects in model/import tests.
- Design doc explains lower priority vs Observer core.
