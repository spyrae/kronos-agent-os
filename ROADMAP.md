# Kronos Agent OS Roadmap

KAOS is moving from a powerful local agent repo toward a clean open-source
Agent OS: easy to try, safe by default, extensible through skills/MCP, and
observable through a local control room.

## 0.1 Public Launch

- Safe offline demo and `kaos doctor`
- Conservative capability gates for dynamic tools, dynamic MCP, and server ops
- Public workspace template and gitignored live workspaces
- Docker Compose quickstart with localhost-only bindings
- Dashboard settings view with visible capability gate status
- Community files, issue templates, CI, and package metadata

## 0.2 Developer Experience

- First-run onboarding flow for LLM providers, Telegram, dashboard, and MCP
- More focused example agents and skills
- Better dashboard empty states and guided fixes
- Packaged dashboard static build for release artifacts
- Docs for common local deployment patterns

## 0.3 Runtime Hardening

- Tool permission profiles per agent
- Capability audit history in dashboard
- Structured traces for agent turns, tool calls, and memory writes
- Safer dynamic tool review workflow
- Snapshot/export/import for agent workspaces

## 0.4 Swarm And Automation

- Visual swarm run timeline
- Sub-agent task handoff templates
- Cron job marketplace for common monitors and reports
- MCP server presets with explicit risk labels
- Multi-agent local compose templates

## 1.0 Bar

- One-command local install and demo on macOS/Linux
- Documented stable plugin/skill contracts
- Reproducible release artifacts
- Security review of risky capability gates
- Production-ready docs for self-hosted teams
