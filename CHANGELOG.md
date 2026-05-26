# Changelog

All notable changes to Kronos Agent OS are documented here.

## [0.2.0] - 2026-05-26

### Added

- **Analytics pipeline** — 11 data sources aggregated into the daily pulse
  and weekly business report: Zabbix, Grafana, Sentry, PostHog (HogQL),
  App Store + Play Store, Supabase stats, Yandex Metrika + GA4, RevenueCat,
  LiteLLM, Langfuse, Linear. Each source is independent and degrades
  gracefully when its credentials are missing.
- **App Store Connect API integration** — `app_store._fetch_ios_reviews`
  pulls recent customer reviews via ASC JWT (ES256) when `ASC_KEY_ID`,
  `ASC_ISSUER_ID`, and `ASC_PRIVATE_KEY_PATH` are configured. New optional
  extra: `pip install kronos-agent-os[appstore]` for `PyJWT[crypto]`.
- **Exa Search fallback for Brave** — `kronos.tools.brave.search()` now
  routes to `kronos.tools.exa` automatically on `HTTP 402` (quota) or
  `HTTP 429` (rate-limit), with a 6-hour sticky cooldown so the same
  failure doesn't keep retrying. Same `SearchResult` dataclass — callers
  need no changes.
- **Telegram Markdown → HTML normaliser** in `cron.notify._sanitize_html`:
  LLMs almost always emit `**bold**` and `### headings` even when asked
  for HTML, so the sender now converts before delivery. Idempotent.
- **SEO/GEO tracker** module — daily Google Search Console refresh plus
  weekly full position + AI citation check, with EXA fallback for search
  and OpenRouter fallback for LLM engines.
- New optional dependencies: `[analytics]` (google-analytics-data for
  GA4), `[seo_geo]` (Search Console + GA4 admin clients).

### Changed

- **Competitor monitoring consolidated to weekly** (Sunday 10:00 UTC).
  Replaced the daily digest and 4-hourly critical alerts with a single
  deep weekly report that runs the full intelligence cycle in one pass:
  fresh fetch across all 8 channels (App Store, Play, website, blog
  RSS, Twitter, press, ProductHunt, jobs), 7-day aggregate, richer LLM
  prompt with per-competitor breakdown and channel/severity histograms,
  competitive-advantage tracker + Mem0 updates.
- **PostHog source** switched from the deprecated
  `/api/projects/<id>/insights/trend/` endpoint to the HogQL Query API
  (`/api/projects/<id>/query/`). Personal API keys now need scope
  `query:read`.
- **Grafana queries** now target real scraped metrics
  (`langfuse_requests_total`, `langfuse_latency_p95`, `mcp_requests_total`)
  instead of the generic `http_requests_total` that most deployments
  don't expose.
- **LiteLLM source** uses the current `/global/spend/logs` and
  `/global/spend/models` endpoints (the old `/spend/logs` path was
  removed in LiteLLM v1.40+).
- **Stricter jobs-channel filter** in competitor monitoring — only counts
  hits on known job boards (LinkedIn / Greenhouse / Lever / Ashby /
  Wellfound / YC Jobs / Indeed posting URL / Glassdoor job listing /
  `/careers/` or `/jobs/` paths) with hiring keywords. Listicles and
  "best apps" articles that mention competitors in passing are skipped.

### Fixed

- `app_store.py` referenced `os.environ.get` without `import os`,
  crashing the source on first import.
- `notify.send_bot_api` ran `_sanitize_html` only on the Bot API
  delivery path; the webhook fallback (used when `TG_BOT_TOKEN` is
  unset — typical for per-agent userbots) received raw Markdown.
- LiteLLM source previously reported `top_models: unknown`, `tokens=0`,
  `latency=None` because it tried to derive these from `/global/spend/logs`,
  which only returns daily spend totals. Now uses the dedicated
  `/global/spend/models` endpoint for the per-model breakdown.
- Browser-Integrity-Check-style Cloudflare 1010 challenges on
  third-party hosts are now bypassed via a browser-like `User-Agent`
  on Langfuse / LiteLLM clients.

### Removed

- Daily competitor digest (`competitor-digest` cron) and the 4-hourly
  `competitor-alerts` cron, replaced by the consolidated weekly report.
  If you relied on these schedules, the same coverage is now delivered
  once per week with deeper analysis.

### Operations

- Snapshot retention: `competitor_snapshots` pruned at 90 days,
  `competitor_changes` at 180 days, alongside the existing
  `swarm_messages` 90-day pruning. All run weekly Sunday 03:00 UTC.

## [0.1.1] - 2026-04-28

### Added

- Telegram topic routing for group chats with multiple threads.
- Telegram session sidecar preservation across restarts.
- Codex OAuth integration for orchestrator routing.
- Expanded agent runtime capabilities.
- PyPI badges and `pip install kronos-agent-os` quickstart in README.

### Fixed

- Telegram model identity now correctly replies under multi-agent setups.
- Competitor monitor startup restored.
- Deploy health check retry logic.
- Peer messages in owner-only topics correctly ignored.

### Changed

- Hardened Codex and MCP runtime config.
- Trimmed deploy sync artifacts for faster deployments.

## [0.1.0] - 2026-04-27

Initial public release.

### Added

- Custom ReAct-style engine.
- Main agent pipeline: validate, memory, route, store, compact.
- Pydantic settings configuration.
- Session memory, FTS5 recall, Mem0 vector memory, and knowledge graph.
- Workspace-local skills and references.
- MCP and custom tool gateway.
- Scheduled jobs for digests, monitoring, analytics, and maintenance.
- Dashboard/API for runtime inspection.
- Optional swarm coordination with SQLite claim arbitration.
- Telethon userbot bridge, Discord bridge, webhook server.
- Public CLI: `kaos doctor`, `kaos init`, `kaos demo`, `kaos chat`, `kaos connect telegram`.
- One-shot chat mode: `kaos chat --prompt` and `--no-memory`.

### Changed

- Reframed the project as Kronos Agent OS (KAOS), not only swarm/council coordination.
- Made `kaos demo` an offline deterministic walkthrough (no Telegram, Docker, or LLM keys required).
- Made live `workspaces/<agent>/` local runtime state; only `workspaces/_template` is public.
- Hardened dashboard defaults: localhost binding and generated password when unset.
- Made Docker quickstart safer with localhost-only port bindings and `.dockerignore`.
- Sanitized public examples, docs, scripts, systemd units, ASO defaults, and dashboard labels.

### Security

- Prompt injection shield, output validation, cost guardrails, and loop detection.
- Dynamic tools, dynamic MCP management, dynamic MCP registry loading, and server ops disabled by default.
- Telegram DMs blocked unless `ALLOWED_USERS` is set or `ALLOW_ALL_USERS=true`.
- Server operations require explicit opt-in plus a private `servers.yaml`.

### Testing

- Regression coverage for capability gates, Docker quickstart, offline demo, CLI parsing, and public workspace surface.
