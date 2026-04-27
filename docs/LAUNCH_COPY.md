# KAOS Launch Copy

Use this as the canonical public copy kit for Kronos Agent OS (KAOS).

First paragraph:

> Kronos Agent OS (KAOS) is a self-hosted runtime for durable AI agents: local memory, reusable skills, MCP tools, scheduled jobs, a dashboard control room, and optional sub-agent coordination.

Repository: `https://github.com/spyrae/kronos-agent-os`

Quickstart:

```bash
git clone https://github.com/spyrae/kronos-agent-os.git
cd kronos-agent-os
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
kaos demo
kaos demo-seed --reset
```

Demo links:

- Durable agent: `docs/DEMO.md`
- Personal operator: `docs/PERSONAL_OPERATOR_DEMO.md`
- Swarm mode: `docs/SWARM_DEMO.md`
- Security model: `docs/SECURITY.md`

## X Thread

1. I am open-sourcing Kronos Agent OS (KAOS): a self-hosted runtime for durable AI agents.

It is not just a chat wrapper. KAOS combines memory, skills, MCP tools, scheduled jobs, a local dashboard, and optional sub-agent coordination.

2. The mental model: an operating layer around an agent.

Runtime handles sessions and connectors. Memory keeps durable context. Skills package reusable behavior. Tools/MCP are auditable. Jobs run scheduled work. Dashboard makes it inspectable.

3. The first demo is a durable local agent.

It recalls safe fixture memory, loads a reviewed skill, logs tool calls, blocks risky MCP mutation by default, shows scheduled jobs, and exposes everything in the control room.

Demo: `docs/DEMO.md`

4. The second demo is a personal operator.

It uses the Personal Operator template to turn safe inbox/task/research fixtures into decisions, risks, and next actions without touching private accounts.

Demo: `docs/PERSONAL_OPERATOR_DEMO.md`

5. Swarm is included, but it is not the whole product.

Use it when roles help: researcher, critic, operator, synthesizer. Keep simple tasks single-agent.

Demo: `docs/SWARM_DEMO.md`

6. The public defaults are conservative.

Dynamic tools: off. Dynamic MCP management: off. Persisted dynamic MCP servers: off. Server ops: off. Telegram DMs blocked until allowlisted.

Security: `docs/SECURITY.md`

7. Try it locally:

```bash
git clone https://github.com/spyrae/kronos-agent-os.git
cd kronos-agent-os
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
kaos demo
```

8. If you want to build agents you can inspect and operate, KAOS is the direction I wanted: local-first, file-backed, dashboard-visible, and serious about tool safety.

GitHub: `https://github.com/spyrae/kronos-agent-os`

## Hacker News

Title:

```text
Show HN: KAOS, a self-hosted runtime for durable AI agents
```

Post:

```text
Hi HN, I am open-sourcing Kronos Agent OS (KAOS), a self-hosted runtime for durable AI agents.

KAOS is meant to feel less like a chatbot demo and more like an operating layer around an agent: local sessions, durable memory, reusable skills, MCP/custom tools, scheduled jobs, a dashboard control room, and optional sub-agent coordination.

The repo includes a deterministic offline demo, dashboard seed state, safe templates, skill packs, and demo assets. The public defaults are intentionally conservative: dynamic tools, dynamic MCP management, persisted dynamic MCP servers, and server ops are disabled unless explicitly enabled.

Quickstart:

git clone https://github.com/spyrae/kronos-agent-os.git
cd kronos-agent-os
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
kaos demo

The dashboard demo can be seeded with:

kaos demo-seed --reset

I would especially appreciate feedback on setup friction, the safety model, and whether the Agent OS framing is clear.
```

## Reddit Variants

### r/selfhosted

```text
I am open-sourcing KAOS, a self-hosted runtime for durable AI agents.

It focuses on local control: file-backed workspaces, local SQLite state, memory inspection, scheduled jobs, MCP/tool visibility, and a dashboard control room. Risky capabilities are off by default.

Quickstart is local:

pip install -e ".[dev]"
kaos demo
kaos demo-seed --reset

Repo: https://github.com/spyrae/kronos-agent-os
Docs: docs/README.md
Security model: docs/SECURITY.md
```

### r/LocalLLaMA

```text
KAOS is a self-hosted Agent OS runtime: sessions, memory, skills, MCP tools, scheduled jobs, dashboard, and optional sub-agent coordination.

It is not model-specific; the interesting part is the operating layer around the agent. I am looking for feedback on the runtime boundaries, memory/tool audit model, and how local-first users would want templates/skills packaged.

Try the deterministic demo:

kaos demo
kaos demo-seed --reset

Repo: https://github.com/spyrae/kronos-agent-os
```

### r/opensource

```text
I am releasing Kronos Agent OS (KAOS), an MIT-licensed self-hosted runtime for durable AI agents.

It includes CLI demos, templates, skill packs, MCP/tool gates, scheduled jobs, a dashboard control room, and optional swarm coordination. The goal is a practical foundation people can inspect, fork, and extend.

Repo: https://github.com/spyrae/kronos-agent-os
Start with: kaos demo
```

## Short Announcement

```text
Kronos Agent OS (KAOS) is now open source.

KAOS is a self-hosted runtime for durable AI agents: memory, skills, MCP/tools, scheduled jobs, dashboard control room, and optional sub-agent coordination.

Start locally:
pip install -e ".[dev]"
kaos demo

Repo: https://github.com/spyrae/kronos-agent-os
```

## Newsletter / Discord / Slack

```text
I am sharing the first public version of Kronos Agent OS (KAOS).

The thesis: useful agents need more than a prompt. They need memory you can inspect, skills you can review, tools you can audit, scheduled work you can operate, and a dashboard that shows what happened.

KAOS includes safe local demos, public templates, skill packs, MCP/tool gates, scheduler visibility, and optional swarm coordination. Public defaults keep risky capabilities disabled until explicit local opt-in.

GitHub: https://github.com/spyrae/kronos-agent-os
Quickstart: kaos demo
Dashboard seed: kaos demo-seed --reset
```

## Maintainer Reply Snippets

### "Is this just LangGraph?"

KAOS can use graph-style agent patterns, but the project scope is the operating layer around the agent: sessions, memory stores, skills, MCP/tool gateways, scheduled jobs, audit trail, dashboard, templates, and optional swarm coordination.

### "Why self-hosted?"

The target user should be able to inspect state, keep memory local, decide which tools exist, and control risky capabilities. Hosted agents can be useful, but this repo optimizes for local ownership and hackability.

### "Is swarm the main feature?"

No. Swarm is an optional coordination mode inside KAOS. The core product is the Agent OS runtime: memory, skills, MCP/tools, automations, dashboard, and safety gates. Use swarm only when role specialization adds value.

### "How do you handle security?"

Fresh installs are conservative. Dynamic tools, dynamic MCP management, persisted dynamic MCP servers, and server ops are disabled by default. Tool calls are redacted and audited. The dashboard approval queue records intent but does not silently flip dangerous env flags.

### "Why a dashboard?"

Durable agents need observability. The dashboard makes memory, tool calls, approvals, jobs, sessions, and swarm coordination inspectable instead of hiding them in logs.

### "Why MCP?"

MCP is a useful standard interface for tools, but KAOS treats it through capability gates and audit logs. Static MCP tools can be configured; dynamic MCP management is blocked unless explicitly enabled in a trusted local deployment.

## Release Notes Draft

````markdown
## v0.1.0

Initial public release of Kronos Agent OS (KAOS).

Highlights:
- Local CLI demo and dashboard seed flow.
- Agent runtime with sessions, memory, skills, MCP/custom tools, scheduled jobs, and optional swarm coordination.
- Dashboard control room for overview, memory, jobs, audit trail, approvals, config, and swarm visualizer.
- Public templates and skill packs.
- Conservative safety defaults and documented threat model.

Start:

```bash
pip install -e ".[dev]"
kaos demo
kaos demo-seed --reset
```

Read next:
- `docs/DEMO.md`
- `docs/SECURITY.md`
- `ROADMAP.md`
````
