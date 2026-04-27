# KAOS Documentation

Start here if you want to understand or contribute to Kronos Agent OS.

## Core Docs

| Doc | Purpose |
|-----|---------|
| [Landing](LANDING.md) | Portable product landing content for launch traffic |
| [Architecture](ARCHITECTURE.md) | System map and Agent OS mental model |
| [Demo](DEMO.md) | Durable agent demo script, commands, and launch assets |
| [LLM Providers](LLM_PROVIDERS.md) | Bring your own model: OpenAI, OpenRouter, Groq, LiteLLM, Ollama, custom endpoints |
| [Personal Operator Demo](PERSONAL_OPERATOR_DEMO.md) | Relatable inbox/task/research demo using the Personal Operator template |
| [Swarm Mode Demo](SWARM_DEMO.md) | Optional multi-agent coordination demo with roles, arbitration, and synthesis |
| [Launch Copy](LAUNCH_COPY.md) | X/HN/Reddit/announcement copy and technical reply snippets |
| [v0.1.0 Release Notes](RELEASE_NOTES_v0.1.0.md) | Release draft with quickstart, safety defaults, and known limitations |
| [Optional Feedback](SOFT_LAUNCH.md) | Lightweight feedback capture and triage rules after publishing |
| [Runtime](RUNTIME.md) | CLI, connectors, sessions, and execution lifecycle |
| [Memory](MEMORY.md) | Session memory, hybrid recall, knowledge graph, sleep compute |
| [Skills](SKILLS.md) | Workspace-local reusable procedures |
| [MCP & Tools](MCP.md) | Static MCP, tool gateway, dynamic tool gates |
| [Automations](AUTOMATIONS.md) | Scheduler, jobs, notifications, audit trail |
| [Sub-Agents & Swarm](SWARM.md) | Optional multi-agent coordination |
| [Dashboard](DASHBOARD.md) | Local control room and API map |
| [Deployment](DEPLOYMENT.md) | Local, Docker, systemd, and server ops setup |
| [Security](SECURITY.md) | Threat model and safe defaults |

## First-Time Path

```bash
kaos demo
cp .env.example .env
# edit .env: add one real LLM key, or configure Ollama/local
kaos doctor
kaos templates install personal-operator personal-demo --force
kaos skills install-pack productivity --agent personal-demo --force
AGENT_NAME=personal-demo kaos chat
```

Then read:

1. [LLM Providers](LLM_PROVIDERS.md)
2. [Runtime](RUNTIME.md)
3. [Security](SECURITY.md)

## Reading Paths

| Goal | Read |
|------|------|
| Run KAOS locally | LLM Providers, Runtime, Deployment, Security |
| Extend the agent | Skills, MCP & Tools, Memory |
| Operate the dashboard | Dashboard, Automations, Security |
| Understand multi-agent mode | Architecture, Sub-Agents & Swarm |
| Contribute code | Architecture, Runtime, Contributor Map |

## Contributor Map

| Area | Primary Files | Notes |
|------|---------------|-------|
| CLI and quickstart | `kronos/cli.py`, `tests/test_cli_demo.py` | keep commands deterministic and safe |
| Runtime pipeline | `kronos/graph.py`, `kronos/engine.py`, `tests/test_engine.py` | validation, routing, tool calls, persistence |
| Memory | `kronos/session.py`, `kronos/memory/`, `tests/test_memory.py` | local stores, recall, compaction |
| Skills | `kronos/skills/`, `workspaces/_template/` | reusable local procedures |
| Templates and packs | `templates/agents/`, `templates/skill-packs/` | launch-ready extension examples |
| MCP/tools | `kronos/tools/`, `tests/test_mcp_smoke.py` | capability gates and tool execution |
| Automations | `kronos/cron/`, `docs/CRON-JOBS.md` | scheduled jobs and notifications |
| Dashboard API | `dashboard/`, `tests/test_dashboard_config.py` | local control-room backend |
| Dashboard UI | `dashboard-ui/` | React UI, Node 18.18+ |
| Sub-agents/swarm | `kronos/group_router.py`, `kronos/swarm_store.py`, `tests/test_swarm_store.py` | optional coordination mode |
| Public surface | `README.md`, `.github/`, community files | launch trust and contributor onboarding |

## Contributor Notes

- Keep public docs free of private paths, hosts, IDs, and live workspace state.
- Prefer safe placeholders and local-first examples.
- When a doc mentions a risky capability, include the capability gate that controls it.
