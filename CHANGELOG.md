# Changelog

All notable changes to Kronos Agent OS are documented here.

## [Unreleased]

### Changed

- Reframed the project as Kronos Agent OS (KAOS), not only swarm/council coordination.
- Added public-safe CLI entry points: `kaos doctor`, `kaos init`, `kaos demo`, `kaos chat`, and `kaos connect telegram`.
- Made `kaos demo` an offline deterministic walkthrough that does not require Telegram, Docker, or LLM keys.
- Added one-shot chat mode with `kaos chat --prompt` and `--no-memory`.
- Made live `workspaces/<agent>/` local runtime state; only `workspaces/_template` is public.
- Hardened dashboard defaults: localhost binding and generated password when unset.
- Made Docker quickstart safer with localhost-only port bindings and `.dockerignore`.
- Sanitized public examples, docs, scripts, systemd units, ASO defaults, and dashboard labels.

### Security

- Dynamic tools, dynamic MCP management, dynamic MCP registry loading, and server ops are disabled by default.
- Telegram DMs are blocked unless `ALLOWED_USERS` is set or `ALLOW_ALL_USERS=true`.
- Server operations require explicit opt-in plus a private `servers.yaml`.

### Testing

- Added regression coverage for capability gates, Docker quickstart, offline demo, CLI parsing, and public workspace surface.

## [0.1.0] - 2026-04-26

Initial source release lineage.

### Core

- Custom ReAct-style engine.
- Main agent pipeline: validate, memory, route, store, compact.
- Pydantic settings configuration.

### Agent OS Modules

- Session memory, FTS5 recall, Mem0 vector memory, and knowledge graph.
- Workspace-local skills and references.
- MCP and custom tool gateway.
- Scheduled jobs for digests, monitoring, analytics, and maintenance.
- Dashboard/API for runtime inspection.
- Optional swarm coordination with SQLite claim arbitration.

### Transports

- Telethon userbot bridge.
- Discord bridge.
- Webhook server for cron integration.

### Safety

- Prompt injection shield.
- Output validation.
- Cost guardrails.
- Loop detection.
