# Kronos Agent OS

Kronos Agent OS (KAOS) is a self-hosted runtime for durable AI agents. It gives an agent memory, reusable skills, MCP/custom tools, scheduled jobs, a dashboard control room, and optional sub-agent coordination.

![KAOS durable agent demo](assets/kaos-durable-agent-demo.gif)

## Start Locally

```bash
git clone https://github.com/spyrae/kronos-agent-os.git
cd kronos-agent-os
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
kaos demo
```

Dashboard demo:

```bash
kaos demo-seed --reset
AGENT_NAME=demo DB_DIR=data/demo DB_PATH=data/demo/session.db SWARM_DB_PATH=data/demo/swarm.db WORKSPACE_PATH=workspaces/demo kaos dashboard
```

## What KAOS Includes

| Module | What It Does |
|--------|--------------|
| Runtime | Runs local CLI, Telegram/webhook, dashboard, and scheduled-job entry points. |
| Memory | Persists sessions, facts, shared facts, and knowledge graph records that can be inspected. |
| Skills | Packages reusable procedures and references as workspace files. |
| Tool Gateway | Connects MCP/custom tools while logging and redacting tool calls. |
| Automations | Runs recurring jobs through the same agent runtime. |
| Control Room | Shows runtime health, memory, jobs, audit trail, approvals, config, and swarm state. |
| Swarm Mode | Optional role-based coordination for tasks that benefit from independent perspectives. |

## Demos

| Demo | Use It To Show |
|------|----------------|
| [Durable Agent Demo](DEMO.md) | Memory, skills, tool audit, scheduled jobs, and dashboard visibility. |
| [Personal Operator Demo](PERSONAL_OPERATOR_DEMO.md) | A relatable inbox/task/research workflow with safe fixtures. |
| [Swarm Mode Demo](SWARM_DEMO.md) | Researcher, critic, operator, and synthesizer roles with arbitration. |

## Templates And Skills

KAOS includes starter templates for common agent shapes:

- Personal Operator
- Research Agent
- Ops Assistant
- Founder Strategy
- Analyst Reporter

Skill packs provide reusable behavior for research, productivity, ops, content, and finance-lite workflows.

```bash
kaos templates list
kaos templates install personal-operator personal-demo --force
kaos skills packs
kaos skills install-pack productivity --agent personal-demo --force
```

## Security Defaults

Fresh installs are conservative:

```bash
ENABLE_DYNAMIC_TOOLS=false
REQUIRE_DYNAMIC_TOOL_SANDBOX=true
ENABLE_MCP_GATEWAY_MANAGEMENT=false
ENABLE_DYNAMIC_MCP_SERVERS=false
ENABLE_SERVER_OPS=false
```

Telegram DMs are blocked until `ALLOWED_USERS` is configured unless `ALLOW_ALL_USERS=true` is set explicitly.

Read [Security](SECURITY.md) before enabling dynamic tools, dynamic MCP management, server ops, or external message-sending workflows.

## Why It Exists

Useful agents need more than a prompt. They need state you can inspect, procedures you can review, tools you can audit, scheduled work you can operate, and defaults that do not surprise users.

KAOS is designed for people who want to run and extend their own agent runtime instead of treating the agent as an opaque hosted box.

## Contribute

- Start with [Architecture](ARCHITECTURE.md), [Runtime](RUNTIME.md), and [Security](SECURITY.md).
- Use [CONTRIBUTING.md](../CONTRIBUTING.md) for local setup and PR expectations.
- Check [Roadmap](../ROADMAP.md) for current priorities.
- Open issues for setup friction, docs gaps, template ideas, skill pack improvements, and safety concerns.

Repository: `https://github.com/spyrae/kronos-agent-os`
