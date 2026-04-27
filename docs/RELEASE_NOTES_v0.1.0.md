# Kronos Agent OS v0.1.0 Release Notes

Initial public release of Kronos Agent OS (KAOS), a self-hosted runtime for durable AI agents.

## Highlights

- Local CLI demo: `kaos demo`.
- Reproducible dashboard demo state: `kaos demo-seed --reset`.
- Agent runtime with sessions, memory, skills, MCP/custom tools, scheduled jobs, and optional swarm coordination.
- Dashboard control room for overview, memory, jobs, audit trail, approvals, config, and swarm visualizer.
- Public agent templates and skill packs.
- Conservative capability defaults and documented threat model.

## Quickstart

```bash
git clone https://github.com/spyrae/kronos-agent-os.git
cd kronos-agent-os
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
kaos demo
kaos doctor
```

Dashboard demo:

```bash
kaos demo-seed --reset
AGENT_NAME=demo DB_DIR=data/demo DB_PATH=data/demo/session.db SWARM_DB_PATH=data/demo/swarm.db WORKSPACE_PATH=workspaces/demo kaos dashboard
```

## Safety Defaults

Fresh installs are conservative:

```bash
ENABLE_DYNAMIC_TOOLS=false
REQUIRE_DYNAMIC_TOOL_SANDBOX=true
ENABLE_MCP_GATEWAY_MANAGEMENT=false
ENABLE_DYNAMIC_MCP_SERVERS=false
ENABLE_SERVER_OPS=false
ALLOW_ALL_USERS=false
```

Dynamic tools, dynamic MCP management, persisted dynamic MCP servers, and server operations require explicit local opt-in. Telegram DMs are blocked until `ALLOWED_USERS` is configured unless `ALLOW_ALL_USERS=true` is set deliberately.

## Known Limitations

- The project is alpha-quality and intended for local/self-hosted experimentation.
- Dashboard assets are built separately from the Python package.
- Dynamic tools require Docker when sandbox enforcement is enabled.
- Real Telegram, MCP, analytics, and server integrations require local credentials and private config.
- Swarm mode can multiply cost and latency; use it only when role specialization helps.
- Some optional integrations require provider-specific setup and are not part of the offline demo.

## Responsible Use

KAOS can connect to tools, scheduled jobs, memory, and external services. Keep risky capabilities disabled unless the deployment is trusted, inspect tool calls, and do not put private secrets or live workspace state in public issues.

Read before enabling advanced features:

- [Security](SECURITY.md)
- [Deployment](DEPLOYMENT.md)
- [MCP & Tools](MCP.md)
- [Contributing](../CONTRIBUTING.md)
