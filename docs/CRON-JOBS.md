# Kronos Agent OS (KAOS) — Cron Jobs

All core cron jobs are registered in `kronos/cron/setup.py` and run by the built-in async scheduler (`kronos/cron/scheduler.py`). No external dependencies (no APScheduler). Runs inside the main event loop alongside bridges and dashboard.

## Schedule Overview

| # | Name | Schedule | Type | Module |
|---|------|----------|------|--------|
| 1 | heartbeat | Every 30 min | Periodic | `heartbeat.py` |
| 2 | news-monitor | Daily 00:00 UTC (08:00 UTC+8) | Daily | `news_monitor.py` |
| 3 | personal-observer | Daily 23:00 UTC (07:00 UTC+8) | Daily | `personal_observer.py` |
| 4 | group-digest | Daily 01:00 UTC (09:00 UTC+8) | Daily | `group_digest.py` |
| 5 | signal-jobs | Paused | Daily | `signal_jobs.py` |
| 6 | signal-ideas | Daily 04:00 UTC (12:00 UTC+8) | Daily | `signal_ideas.py` |
| 7 | signal-travel-insights | Paused | Daily | `signal_travel.py` |
| 8 | daily-scope | Daily 14:00 UTC (22:00 UTC+8) | Daily | `personal_observer.py` |
| 9 | email-expenses | Daily 00:00 UTC (08:00 UTC+8) | Daily | `email_expenses.py` |
| 10 | sleep-compute | Daily 03:00 UTC (11:00 UTC+8) | Daily | `sleep_compute.py` |
| 11 | self-improve | Daily 22:00 UTC (06:00 UTC+8) | Daily | `self_improve.py` |
| 12 | expense-digest | Weekly Sun 02:00 UTC (10:00 UTC+8) | Weekly | `expense_digest.py` |
| 13 | people-scout | Weekly Sun 02:00 UTC (10:00 UTC+8) | Weekly | `people_scout.py` |
| 14 | skill-improve | Weekly Sun 20:00 UTC (04:00 UTC+8) | Weekly | `skill_improve.py` |
| 15 | user-model | Weekly Wed 20:00 UTC (04:00 UTC+8) | Weekly | `user_model.py` |
| 16 | market-review | Weekly Fri 10:00 UTC (18:00 UTC+8) | Weekly | `market_review.py` |
| 17 | source-quality-audit | Weekly Sun 04:00 UTC with 13-day guard | Weekly | `source_quality_audit.py` |
| 18 | swarm-retention | Weekly Sun 03:00 UTC (11:00 UTC+8) | Weekly | `swarm_retention.py` |
| 19 | turn-retention | Weekly Sun 05:00 UTC (13:00 UTC+8) | Weekly | `turn_retention.py` |
| 20 | swarm-weekly-report | Weekly Sun 06:00 UTC (14:00 UTC+8) | Weekly | `swarm_weekly.py` |
| 21 | sla-escalation | Every 60 s | Periodic | `escalation.py` |
| 22 | acquire-smoke | Daily 06:00 UTC (14:00 UTC+8) | Daily | `acquire_smoke.py` |

Intake pollers registered alongside these: `user-reminders` (60 s),
`handoff-intake`, `council-intake`, `memory-intake` (30 s each).

## Job Details

### 1. heartbeat
**Schedule:** Every 30 minutes
**Module:** `kronos/cron/heartbeat.py`

Reads `workspace/HEARTBEAT.md` tasks + queries Notion DB for current tasks. Sends to DeepSeek (lite) for analysis. Only notifies if something actionable (overdue deadlines, important reminders). Silent if everything is fine ("heartbeat: ok").

**Dependencies:** HEARTBEAT.md, Notion API (optional)
**Notification:** Webhook → Telegram DM

### 2. news-monitor
**Schedule:** Daily 00:00 UTC
**Module:** `kronos/cron/news_monitor.py`

Daily news digest pipeline:
1. Load `news` sources from the Signal Intelligence registry.
2. Fetch X/Reddit/Telegram/search candidates.
3. Score and cluster items with evidence-aware guardrails.
4. Render a Telegram HTML digest with confirmed/emerging/anecdotal sections.
5. Send to Telegram via Bot API (`TOPIC_DIGEST_NEWS`, fallback `TOPIC_DIGEST`)

**Dependencies:** BRAVE_API_KEY, Telethon session for private Telegram sources
**Notification:** Bot API → Telegram `Digest: News` topic

### 3. group-digest
**Schedule:** Daily 01:00 UTC
**Module:** `kronos/cron/group_digest.py`

Daily Telegram group digest:
1. Load groups from `workspace/skills/group-digest/references/GROUPS.md`
2. For each group: fetch last 24h messages via Telethon (max 200 per group)
3. Filter significant messages by engagement (reactions >= 3 or views >= 200)
4. Score and rank: `reactions * 10 + views / 100`
5. LLM synthesis (DeepSeek lite) → HTML digest with insights
6. Send to Telegram via Bot API (`TOPIC_DIGEST_NEWS`, fallback `TOPIC_DIGEST`)

**Dependencies:** Telethon client (shared), GROUPS.md
**Notification:** Bot API → Telegram `Digest: News` topic

### personal-observer
**Schedule:** Daily 23:00 UTC (07:00 UTC+8)
**Module:** `kronos/cron/personal_observer.py`

Morning Observer digest for private Telegram dialogs:
1. Connect to the Telethon userbot session.
2. Read unread private dialogs via the read-only Observer scanner.
3. Detect reply debts deterministically without LLM calls.
4. Render a compact Telegram HTML digest.
5. Send via Bot API / default notification destination.

Scheduled at 23:00 UTC to avoid existing 00:00 UTC news/email jobs and the
01:00 UTC group digest. Runs only on the `kronos` agent.

**Dependencies:** Telethon userbot session
**Notification:** Bot API → default notification chat

### daily-scope
**Schedule:** Daily 14:00 UTC (22:00 UTC+8)
**Module:** `kronos/cron/personal_observer.py`

Evening private-dialog map for the current local day:
1. Read private dialog snapshots without read acknowledgements.
2. Build deterministic per-contact summaries from limited excerpts.
3. Detect agreement markers (`договорились`, `жду`, `скинь`, `напомни`, `давай`, `сделаю`).
4. Flag risk when the last message is incoming.
5. Save `workspace/notes/user/daily-scope/YYYY-MM-DD.md` and send Telegram HTML.

**Dependencies:** Telethon userbot session
**Notification:** Bot API → default notification chat

### 4. signal-jobs
**Schedule:** Daily 02:00 UTC
**Module:** `kronos/cron/signal_jobs.py`

Signal Intelligence hiring digest:
1. Load `jobs` sources from the Signal Intelligence registry.
2. Fetch Telegram/Reddit/X/search candidates.
3. Filter generic listicles and keep direct hiring/actionable role signals.
4. Render evidence-aware clusters.
5. Send to Telegram via Bot API (`TOPIC_DIGEST_JOBS`, fallback `TOPIC_DIGEST`).

**Notification:** Bot API → Telegram `Digest: Jobs` topic

### 5. signal-ideas
**Schedule:** Daily 04:00 UTC
**Module:** `kronos/cron/signal_ideas.py`

Product/business ideas digest:
1. Load `ideas` sources from the Signal Intelligence registry.
2. Detect JTBD phrasing, repeated pain points, founder insights, and product-launch opportunities.
3. Filter promos/listicles/noise.
4. Render 5–10 evidence-ranked ideas with pain/opportunity, product angle, why-now, confidence caveat, and guardrail language.
5. Send to Telegram via Bot API (`TOPIC_DIGEST_IDEAS`, fallback `TOPIC_DIGEST`).

**Notification:** Bot API → Telegram `Digest: Product/Business Ideas` topic

### 6. signal-travel-insights
**Schedule:** Paused since 2026-07-03
**Module:** `kronos/cron/signal_travel.py`

Disabled: collection, analysis, and Telegram publication to
`JB: Travel Insights` are currently stopped.

JourneyBay travel insights digest:
1. Load `travel_insights` sources from the Signal Intelligence registry.
2. Fetch travel Reddit/search, competitor changes, and owned review/status sources when adapters are available.
3. Filter generic travel news/destination content and keep pain/feature/workflow signals.
4. Render evidence-ranked insights with problem/pain, JourneyBay implication, caveat, and trend guardrails.
5. Send to Telegram via Bot API (`TOPIC_JB_TRAVEL_INSIGHTS`, fallback `TOPIC_DIGEST`).

**Notification:** Bot API → Telegram `JB: Travel Insights` topic

### 7. email-expenses
**Schedule:** Daily 00:00 UTC
**Module:** `kronos/cron/email_expenses.py`

Auto-extract expenses from Gmail receipts:
1. Search Gmail for receipt/invoice emails through Google Workspace MCP.
2. LLM extracts expense data (description, amount, currency, category, date).
3. Create entries through the canonical `add_expense` tool, so RUB/IDR
   handling and budget FIFO stay consistent with manual expense logging.

**Dependencies:** NOTION_API_KEY, NOTION_EXPENSES_DB_ID, Google Workspace OAuth, LITE LLM provider
**Status:** MCP-backed best-effort; skips safely when any dependency is missing
**Notification:** Webhook → Telegram DM

### 8. sleep-compute
**Schedule:** Daily 03:00 UTC (L4 Memory)
**Module:** `kronos/cron/sleep_compute.py`

Nightly memory consolidation:
1. Get recent facts from FTS5 (last 7 days)
2. LLM extracts entities and relationships → Knowledge Graph (DeepSeek lite)
3. Build/update entity relations in SQLite
4. Generate 1-3 actionable insights from graph patterns
5. Clean up stale facts (>90 days) from FTS5

**Dependencies:** FTS5 database, Knowledge Graph database
**Notification:** Webhook → Telegram DM (entities added, relations, insights)

### 9. self-improve
**Schedule:** Daily 22:00 UTC
**Module:** `kronos/cron/self_improve.py`

Daily agent self-improvement:
1. Read last 24h from `audit.jsonl` (up to 20 entries)
2. Load previous improvements from `workspace/memory/self-improve/`
3. LLM analyzes sessions → proposes ONE concrete improvement (DeepSeek lite)
4. Save as dated learning record (YYYY-MM-DD.md)
5. Skip if "no improvements needed"

**Dependencies:** audit.jsonl
**Notification:** Webhook → Telegram DM

### 10. expense-digest
**Schedule:** Weekly Sunday 02:00 UTC
**Module:** `kronos/cron/expense_digest.py`

Weekly expense report:
1. Query Notion Expenses DB for last 7 days
2. LLM analysis (DeepSeek lite): totals, by category, top 3 expenses, trend, recommendation
3. Send HTML report to Telegram

**Dependencies:** NOTION_API_KEY, user-configured expenses database
**Notification:** Bot API → Telegram Finance topic

### 11. people-scout
**Schedule:** Weekly Sunday 02:00 UTC
**Module:** `kronos/cron/people_scout.py`

LinkedIn profile discovery:
1. Rotate focus weekly: US founders → EU founders → AI engineers → Indie hackers
2. LLM generates profiles based on criteria (Sonnet standard)
3. Deduplicate against `SEEN.md`
4. Extract LinkedIn URLs → update SEEN.md
5. Send HTML report to Telegram

**Dependencies:** CRITERIA.md, SEEN.md
**Notification:** Bot API → Telegram Scout topic

### 12. skill-improve
**Schedule:** Weekly Sunday 20:00 UTC
**Module:** `kronos/cron/skill_improve.py`

Auto-improvement of skill files:
1. Read last 7 days from `audit.jsonl`
2. Match interactions to skills by keywords (expense-tracker, investment-analysis, etc.)
3. For skills with >= 3 interactions: LLM proposes minimal improvement (DeepSeek lite)
4. Backup current SKILL.md → `.versions/SKILL.vN.md`
5. Write updated SKILL.md

**Dependencies:** audit.jsonl, skill SKILL.md files
**Notification:** Webhook → Telegram DM

### 13. user-model
**Schedule:** Weekly Wednesday 20:00 UTC
**Module:** `kronos/cron/user_model.py`

Dialectical user modeling:
1. **Quantitative**: Pure Python analytics on audit.jsonl (peak hours, avg message length, tier distribution, response time)
2. **Qualitative**: LLM analyzes last 30 conversations plus decision/preference snippets from session search
3. **Dialectical**: Compare against previous model → validate/update/add hypotheses
4. **Passive quality signals**: correction requests, slow responses, tool-heavy sessions, errors, and cost patterns without requiring likes/reactions
5. Categories: Beliefs, Motivations, Decision Patterns, Tensions, Evolution
6. Each belief has numeric confidence: 0.0-1.0
7. Save to `workspace/USER-MODEL.md` and `workspace/USER-PATTERNS.md`

**Dependencies:** audit.jsonl, session search index, USER-MODEL.md (previous model)
**Notification:** Webhook → Telegram DM

### 14. market-review
**Schedule:** Weekly Friday 10:00 UTC
**Module:** `kronos/cron/market_review.py`

Weekly investment market review:
1. Load tickers from `workspace/skills/investment-analysis/references/WATCHLIST.md`
2. Brave Search for news per ticker (freshness=past week, up to 10 tickers)
3. LLM synthesis (standard tier): market overview, per-ticker events + sentiment, next week watch items
4. Send HTML report to Telegram

Safety: this is a watchlist brief, not individualized financial advice. The
prompt explicitly forbids direct buy/sell commands and asks for
`monitor/review thesis/reduce risk/wait for data` style actions.

**Dependencies:** BRAVE_API_KEY, WATCHLIST.md
**Notification:** Bot API → Telegram Finance topic

### 15. source-quality-audit
**Schedule:** Weekly Sunday 04:00 UTC with a 13-day guard
**Module:** `kronos/cron/source_quality_audit.py`

Biweekly Signal Intelligence source quality audit:
1. Read `source_quality_stats` from the signals store.
2. Calculate fetched/accepted/duplicate/low-confidence/cluster/digest contribution metrics.
3. Render keep/promote/demote/quarantine recommendations with concrete evidence.
4. Save the audit artifact without mutating `SOURCES.yaml`.
5. Send to Telegram via Bot API (`TOPIC_DIGEST_NEWS`, fallback `TOPIC_DIGEST`).

**Notification:** Bot API → Telegram `Digest: News` topic

### 16. swarm-retention
**Schedule:** Weekly Sunday 03:00 UTC
**Module:** `kronos/cron/swarm_retention.py`

Retention cleanup:
1. Prune old swarm ledger messages after the configured retention window.
2. Prune old competitor snapshots/changes beyond retention.
3. Run safely on all agents; empty per-agent stores are harmless.

**Notification:** Silent unless logs/errors require investigation.

### 19. turn-retention
**Schedule:** Weekly Sunday 05:00 UTC
**Module:** `kronos/cron/turn_retention.py`

Prunes finished durable turns and their journal / tool results / effects rows per
`policy.retention.turn_journal_days`. A turn's effects are dropped with the turn
because the default idempotency key contains its `turn_id`.

**Notification:** Silent unless logs/errors require investigation.

### 20. swarm-weekly-report
**Schedule:** Weekly Sunday 06:00 UTC
**Module:** `kronos/cron/swarm_weekly.py`

The same post-mortem as `kaos swarm report --week`: who answered and at which
tier, spend and cost per reply, reactions, owned-topic activity and missed SLAs,
hand-offs, councils and reviews.

All six processes run this job, so it first claims the ISO week in the shared
`job_claims` table — the one whose INSERT won sends, the rest return. A week with
no replies and no watched topics is not pushed at all.

**Notification:** Bot API → Telegram `Digest` topic + NTFY summary.

### 21. sla-escalation
**Schedule:** Every 60 seconds
**Module:** `kronos/cron/escalation.py`

For each `sla_watch` row whose deadline passed with no answer, hands the topic to
the owner's `escalates_to` through the existing hand-off queue. An owner that
answered late closes the watch as `answered`; an owner with no cover is dropped
once and counted rather than rechecked forever. Cross-process safety comes from a
compare-and-set on the watch row, so exactly one of the six pollers creates the
hand-off. A no-op ledger read when `agents.yaml` declares no ownership.

**Notification:** none directly — delivery is the hand-off itself.

### 22. acquire-smoke
**Schedule:** Daily 06:00 UTC (14:00 UTC+8)
**Module:** `kronos/cron/acquire_smoke.py`

Fetches a known-good page through each acquisition tier (`plain`, `stealth`,
`browser`) and records which ones worked. Guards internally to the `kronos`
agent: the backends are per-host, so six probes would be five wasted browser
launches and five duplicate alerts about one fault.

It exists because each tier degrades quietly — the stealth backend lives outside
the repo and a host rebuild removes it, Playwright once deleted the API the
browser tier read pages through — and those turn into "the agent can't read that
site any more" weeks later, blamed on the site. It probes `example.com`, not a
marketplace: the question is whether our machinery works, not whether a
marketplace is in a good mood today. See [ACQUISITION.md](ACQUISITION.md).

A tier that is not installed reports `off`, which is a deployment choice and
never an alert. Run the same probe by hand with `kaos acquire check`.

**Notification:** only when a tier's status *changes* — Telegram webhook plus
NTFY, `high` priority when a working tier was lost, `default` on recovery.
Silence is the normal outcome.

## Scheduler Implementation

The scheduler (`kronos/cron/scheduler.py`) is a lightweight async cron without external dependencies:

- **Periodic jobs**: checked every 30 seconds, run if `interval_seconds` elapsed since last run
- **Daily jobs**: run when `hour == cron_hour` (UTC), at most once per hour
- **Weekly jobs**: additionally checks `weekday == cron_weekday` (0=Monday, 6=Sunday)
- Jobs run as `asyncio.create_task()` — non-blocking
- Initial 30-second delay after startup for bridge/webhook to be ready
- Jobs cannot overlap (flag `_running` prevents re-entry)

## Notification Methods

Two delivery methods in `kronos/cron/notify.py`:

| Method | When | How |
|--------|------|-----|
| `send_webhook()` | Simple notifications | POST to local bridge webhook (port 8788) |
| `send_bot_api()` | Topic messages | Direct Telegram Bot API (supports `message_thread_id`) |

Both support message chunking (4000 char limit) and parse_mode (HTML).

## Topic Destinations

`TOPIC_DIGEST` remains the backward-compatible fallback. New signal/JourneyBay
topics can be configured independently:

| Env var | Destination | Owner agent env/default | Current producers |
|---------|-------------|-------------------------|-------------------|
| `TOPIC_DIGEST_NEWS` | `Digest: News` | `TELEGRAM_DIGEST_NEWS_AGENT=kronos` | `news-monitor`, `group-digest` |
| `TOPIC_JB_COMPETITORS` | `JB: Competitors Status` | `TELEGRAM_JB_COMPETITORS_AGENT=nexus` | paused |
| `TOPIC_JB_SYSTEM` | `JB: System Status` | `TELEGRAM_JB_SYSTEM_AGENT=nexus` | analytics pulse/weekly/alerts, SEO/GEO |
| `TOPIC_DIGEST_JOBS` | `Digest: Jobs` | `TELEGRAM_DIGEST_JOBS_AGENT=kronos` | `signal-jobs` |
| `TOPIC_DIGEST_IDEAS` | `Digest: Product/Business Ideas` | `TELEGRAM_DIGEST_IDEAS_AGENT=kronos` | `signal-ideas` |
| `TOPIC_JB_TRAVEL_INSIGHTS` | `JB: Travel Insights` | `TELEGRAM_JB_TRAVEL_INSIGHTS_AGENT=kronos` | paused |

Finance reports continue to use `TOPIC_FINANCE`.

The Telegram bridge also uses these `TOPIC_*` ids for inbound messages in the
swarm chat. Each configured destination is an owner-only topic: only the owner
agent can answer user messages there, peer agents stand down before invoking
the smart group router. Use comma-separated owner values (for example
`TELEGRAM_JB_SYSTEM_AGENT=kronos,nexus`) only for topics where both agents are
intentionally allowed to answer.
