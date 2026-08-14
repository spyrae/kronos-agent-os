# Changelog

All notable changes to Kronos Agent OS are documented here.

## [Unreleased]

### Added

- **Marketplace skill pack** — the five procedures the tools were built for:
  `housing-search`, `marketplace-compare`, `price-watch`,
  `seller-correspondence` and `structured-extraction`. They encode the domain
  rules that decide whether an answer is true rather than plausible: a listing
  that does not state its deposit is not a listing with no deposit; landed cost
  is price plus shipping plus duty plus fees; 4.9 across 3,000 sales beats 5.0
  across 12; a watch fires on crossing a threshold, not on a price being below
  it. The correspondence skill is the one that writes under the owner's name, so
  it says plainly that an instruction found in a listing is data rather than a
  request, and names the five shapes that go to the owner instead of being
  handled. `kaos skills install-pack marketplace --agent <name>`.
- **Repositories the agent may read** — "why did the build break" and "what
  changed this week" are questions with a definite answer, usually asked away
  from the laptop. `kaos repos add <name> <path>` registers a working copy;
  `repo_list` / `repo_tree` / `repo_read` / `repo_search` / `repo_history` /
  `repo_diff` answer from it. Read-only, and the boundary is the feature: nothing
  outside a registered root, every path resolved before it is compared (so a
  symlink out of the tree is refused like `../` is), and credentials refused
  before being read — by name across every path segment, by the repository's own
  gitignore, and never surfaced through a search result. What does get through is
  redacted and marked untrusted, because a code comment is as good a place to
  hide an instruction as a web page. Nothing is written: committing needs
  credentials and a decision about which repositories may change unattended, so a
  permission other than `read` is refused rather than quietly accepted. Docs:
  [Repositories](docs/REPOSITORIES.md).
- **compare_offers — totals as arithmetic, not prose** — "which flat is the
  better deal" is adding money correctly and noticing what is missing, and both
  are where invented numbers come from. The tool ranks on money only and says so
  in its own output; weighing location or seller reputation stays with whoever
  reads it. Three refusals carry the value: an offer missing a cost is set aside
  and named rather than ranked as the cheapest, two currencies are refused
  without a rate this has no business inventing, and one-off costs are separated
  from recurring ones — the difference between the cheapest month and the
  cheapest stay. No sandbox needed: a fixed comparison is a function.
- **run_code — one-off analysis in the sandbox** — the container could previously
  only run *persisted* dynamic tools, so ad-hoc work (normalise forty listings,
  parse an odd export) had nowhere to go and stayed in the model's head. Opt-in
  via `ENABLE_CODE_EXECUTION` / `capabilities.code_execution`, stdlib only, no
  network, stdout is the answer. Each session gets one writable directory that
  outlives the run, defaulting to the current thread — so a plan accumulates its
  own working files and a step three days later reads what an earlier one wrote.
  Docs: [Sandbox](docs/SANDBOX.md).

- **Plans — work that outlives a turn** — a durable turn protects minutes; "ask
  three landlords and compare what comes back" is days, mostly spent waiting. A
  plan is a goal plus steps, where a step holds a prompt, what it depends on, and
  what it waits for: a time, you resuming it, you replying, a page starting or
  stopping to match a pattern, or a number on a page crossing a threshold. The
  poller runs each ready step as an ordinary agent turn, so durable turns, the
  effects ledger, approvals and the cost guardian apply unchanged. Conditions are
  validated in the turn that writes them — a bad regex or a forbidden host is
  refused with the reason, not discovered next Tuesday. A dependency counts as
  satisfied when it *finished*, not when it succeeded, so one silent landlord does
  not turn two answers into none. Steps are silent unless marked `notify`; what
  always arrives is one closing summary. `plan_start` / `plan_add_step` /
  `plan_status` / `plan_cancel`, `kaos plans list/show/resume/cancel`, a dashboard
  page, and bounds on everything that could otherwise wait forever. Docs:
  [Plans](docs/PLANS.md).

- **Site accounts — acting as the owner, without holding the keys** — an account
  says which sites the agent may work on as you, which domains that session may
  be used on, and what it may do there (`read` / `message` / `full`, with
  anything that leaves a trace waiting for approval by default; an unclassified
  action is refused, not assumed harmless). Sessions live in a browser profile
  you sign into by hand; a password stored in the new vault lets the agent sign
  in again by itself when that expires. The password goes from the vault into
  the page and nowhere else — the model is told "ready" or "needs login", never
  a credential or a profile path, which is what a listing page saying *"print
  your credentials"* has to run into. The host is checked before the vault is
  opened and again against the live URL right before typing, so a login flow
  that redirects elsewhere gets nothing. `kaos vault init / status`, page in the
  dashboard, every use of a stored password in the hash-chained
  `credentials.jsonl`. Docs: [Site Accounts](docs/SITE-ACCOUNTS.md).

- **Skill registry with provenance and auto-eval** — a skill can now declare
  `version`, `requires_kaos`, `checksum` and an SSH `signature` (verified through
  `ssh-keygen -Y verify`, no new dependency), and `registry.yaml` lists named
  sources with a trust level. `kaos skills search / info / install / verify /
  approve / stats` work against them; install replays the skill's own scenario
  offline and only a signature from a configured key plus a check that did not
  fail installs a skill active — everything else lands as a reviewable draft with
  the reason attached. A skill with no scenario is marked unverified, not refused.
  Usage counting is local, and the anonymous aggregate is assembled only with
  `registry.telemetry: share`. Docs: [Registry](docs/REGISTRY.md).
- **Measured self-improvement** — every weekly persona proposal now carries a
  measurement (`persona_proposals.eval_json`): the patch is applied to a copy of
  the workspace, the prompt is reassembled and the offline suite runs against both.
  A proposal that breaks prompt assembly, regresses a scenario, or repeats guidance
  already in the file is auto-rejected with a reason and no notification; the rest
  reach the owner with the delta. The report states plainly that scripted scenarios
  cannot score answer quality (`measured_quality: false`) instead of implying a
  number. `/persona show <id>`, `/persona list --rejected`.

- **Swarm 3.0 — the org chart is config** — `agents.yaml` gained `owns`,
  `escalates_to`, `sla_minutes`, `budget_usd_daily`, `dissent` and
  `max_implicit_replies`, validated at startup (a broken `escalates_to` raises;
  contested ownership and over-committed budgets warn). A subject someone owns is
  answered by the owner without a relevance check and on the fast lane, other
  agents defer instead of talking over them, and a missed `sla_minutes` escalates
  to the owner's cover through the existing hand-off queue. Per-agent budgets add
  quiet mode: out of its own money, an agent still answers when addressed but
  stops volunteering. `dissent: require` sends a draft answer to a different role
  before it is sent. New `kaos swarm report [--day|--week] [--json]` and a Sunday
  push, `kaos demo --swarm` to see all of it without Telegram accounts. Docs:
  [Swarm](docs/SWARM.md).
- **Durable execution v2** — an interrupted turn can now be finished instead of
  only reported. New `external_effects` ledger makes re-execution safe (a
  message already sent is not re-sent; `side_effect` + optional
  `idempotency_key` metadata per tool), `durable.resume_mode: resume` opts in,
  and `max_resume_attempts` stops a crash loop. Claiming decides superseded /
  failed / resuming in one transaction. New `kaos turns list/show/fork/resume`,
  a Durable Turns page in the control room, `running_turns` on `/health`, and a
  weekly `turn-retention` job. Docs: [Runtime](docs/RUNTIME.md).
- **Governance as code (`policy.yaml`)** — one validated file for capability
  gates, approval rules, budgets, untrusted-output handling, egress, retention
  and PII masking, with `kaos policy report` printing the effective value and
  its source (env > policy > default). An invalid policy stops startup instead
  of reverting to permissive defaults; absent the file nothing changes. See
  [ADR-0001](docs/decisions/ADR-0001-governance-as-code.md).
- **Untrusted content, closed properly** — every external tool surface is now
  marked untrusted (all MCP tools including after `mcp_reload`, public Telegram
  channels, email-derived expense listings, ingested PDF/DOCX text), not just
  browser tools. Injection attempts inside that content are detected, audited
  and counted, with `log` / `strip` / `block` reactions. Detection patterns
  cover Russian as well as English — the agent's primary language was
  previously unguarded. Corpus: `tests/fixtures/injections.txt` (30 entries).
- **Egress allowlist** — `egress.mode: allowlist` restricts reachable hosts for
  browser navigation and skill imports, and `allowed_commands` restricts stdio
  MCP server commands. `dry_run: true` logs what would be blocked so a rollout
  can be observed before enforcement. localhost and private ranges stay
  reachable; demo mode forces enforcement.
- **Tamper-evident audit** — `tool_calls.jsonl` and `audit.jsonl` are
  hash-chained (`prev_hash` / `entry_hash`); `kaos audit verify` reports the
  first edited, removed or reordered line. Pre-chain entries from an older
  install are skipped and counted rather than reported as tampering.
- **Scheduled pipelines covered** — signal digests (candidate catalog, headline
  block) and competitor page diffs now frame external content as data before it
  reaches a model, and Brave/Exa/`t.me` requests go through the egress
  allowlist. A competitor's page was previously read into an LLM prompt verbatim
  by a weekly job. Injection reaction is shared between the tool loop and the
  pipelines (`security/untrusted.frame_external`) instead of being duplicated.
- New docs: [Governance](docs/GOVERNANCE.md).
- **Agent CI: cassettes, golden scenarios, behaviour diff** — `kaos eval
  capture/run/diff/turns`. Cassettes (`KAOS_CASSETTE_MODE=off|record|replay`)
  give byte-stable replay of provider and tool calls, wired into
  `_get_model_from_chain` and `execute_tool`; a replay miss is a hard error, and
  a tool marked `untrusted_output` must have a cassette (local tools still run).
  Scenarios capture the model turns of a real turn from the durable journal and
  replay them against a scripted model, so tool wiring, call order, approval
  gating and budgets stay checkable across a prompt change. `kaos eval diff`
  compares two revisions structurally (new failures, tool path, gating, turns,
  material answer-size change) by running the base revision in a temporary git
  worktree. New CI job `evals` (`pytest -m eval` + `kaos eval run`, no secrets)
  posts the diff into the PR summary; `make evals` / `make evals-diff` locally.
  Bundled suite: six public-safe scenarios covering the policy surface.
- New docs: [Agent CI](docs/EVALS.md).
- **Agent portability (`.kaos` bundles)** — `kaos export` / `kaos import`
  move an agent's persona, skills, facts, knowledge graph, shared facts and
  pending schedule between installations. Bundles carry a `manifest.json`
  with a SHA-256 per artifact; import verifies it before writing anything and
  aborts on a single altered byte. Exports are deterministic (sorted JSONL,
  fixed archive timestamps), so re-exporting unchanged state yields a
  byte-identical file.
- **Import from other tools** — `kaos import-from <tool|auto> <path>` converts
  a foreign export into a bundle, then runs the same verified import path:
  ChatGPT (`conversations.json`, directory, or export zip — keeps the retained
  branch of each message tree), Claude projects (directory or `projects.json`),
  Obsidian vaults (`[[wikilinks]]` become graph relations), Telegram
  (`result.json`, opt-in per chat), and Letta `.af` agent files.
- `kaos doctor` reports the bundle schema version and available importers.
- New docs: [Portability](docs/PORTABILITY.md).

### Changed

- **The browser tier is installed by deploy, and reads pages through the API that
  still exists.** Two findings from pointing the deployed agent at real
  marketplaces. The host had no Playwright at all, so the last acquisition tier
  and every site session were dead — `deploy.sh` now installs the `browser` extra
  and chromium when they are absent. And current Playwright has no
  `page.accessibility`: `snapshot()` *returned* the resulting error message, so
  the browser tier fed that sentence to the extractor as page content and the
  signed-in check, finding no "log in" in it, said yes on a session that may have
  expired. Snapshots now use `aria_snapshot` with visible text as a fallback, and
  the browser tier returns markup — a price in an attribute is invisible in an
  accessibility tree.
- **Context is compacted by size, not by message count.** Measured before the
  change: five turns carrying a pasted document each are 57,150 tokens and never
  compacted, while sixteen one-line exchanges are 944 tokens and did — spending a
  summarisation call to save nothing. A token budget (`CONTEXT_TOKEN_BUDGET` /
  `budgets.context_tokens`, default 12,000 of persisted history) now drives all
  three strategies; the count trigger keeps a floor so a tiny history is never
  summarised. What survives is trimmed to the budget, and compaction no longer
  depends on Mem0 being configured — deployments with no long-term memory were
  the ones carrying unbounded histories. Tool output had the same bug in
  miniature: whether the model saw all of it was decided by matching the tool's
  *name*, so `run_code`, `plan_status` and `list_site_accounts` matched nothing
  and went in whole at any size. There is now a general ceiling, and a tool whose
  value is being complete declares `output_max_chars=0`.
- **The sandbox now enforces what it declared.** `create_session_workspace` built
  a directory tree that the runner never mounted, so no files went in or out;
  the storage budget was a number in a manifest; and the dynamic-tool path
  assembled its policy from the same request it validated, making an input
  violation unreachable. The session directory is mounted read-write at `/work`
  and is the working directory, a watchdog outside the container enforces the
  disk budget while the run proceeds, and the policy comes from a new `sandbox:`
  section in `policy.yaml`.
- `kronos.audit.redact_secrets` is now public (was `_redact_string`'s inline
  loop) so exports and audit logs share one copy of the credential patterns.

### Fixed

- Two agents could answer each other without end. A peer replying to my message
  sets `reply_to_me` → `explicit_to_me` → Tier 1, and Tier 1 deliberately
  bypasses the cooldown, the implicit-reply cap and arbitration; my answer is
  itself a reply to theirs, so the loop had no exit. Peer-sourced Tier 1 is now
  bounded per window (`MAX_PEER_EXCHANGES` / `PEER_EXCHANGE_WINDOW`); the user's
  explicit address is untouched. Found by the new local swarm bus on its first
  run.
- `kronos.portability.export` and `import_` resolve `kronos.workspace.ws` at
  call time instead of binding it at import time; a module-level binding
  ignored a swapped workspace, which also made an earlier test pass against
  the wrong directory.

### Removed

- **The unsandboxed execution path.** Two places dropped to `exec()` in the
  agent's own process when Docker was missing, both logging "unsafe, dev only"
  and both one environment variable away from running model-written code in
  production. Missing Docker now means the code does not run, and
  `REQUIRE_DYNAMIC_TOOL_SANDBOX=false` turns dynamic tools off rather than
  running them unprotected.

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
