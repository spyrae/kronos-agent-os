# Kronos Agent OS (KAOS) Deployment

KAOS is local-first. Start with CLI/demo mode, then add Telegram, dashboard, MCP, cron jobs, and optional swarm processes as needed.

## Local Runtime

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,memory]"

kaos demo
cp .env.example .env
kaos doctor
kaos chat
kaos dashboard
```

Telegram/userbot mode:

```bash
python scripts/auth-userbot.py
python -m kronos
```

Optional ASO automation dependencies:

```bash
pip install -e ".[aso]"
```

Dashboard UI development requires Node.js 18.18+:

```bash
nvm use
cd dashboard-ui
npm install
npm run dev
```

## Docker Compose

```bash
cp .env.example .env
docker compose up --build
```

The compose file binds exposed ports to `127.0.0.1` on the host. Inside the container, `DASHBOARD_HOST=0.0.0.0` is safe because the host port mapping remains localhost-only.

The default Compose command starts `kaos dashboard`, not the Telegram bridge. This keeps the Docker quickstart usable without Telegram credentials. After provider and Telegram credentials are configured, run the full runtime with `python -m kronos` or override the Compose command.

## Environment

At least one LLM provider is required for chat/runtime use:

```bash
FIREWORKS_API_KEY=fw_...
DEEPSEEK_API_KEY=sk-...
```

Telegram:

```bash
TG_API_ID=12345678
TG_API_HASH=abcdef...
TG_BOT_TOKEN=
ALLOWED_USERS=123456789
ALLOW_ALL_USERS=false
```

Dashboard:

```bash
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8789
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=
```

If `DASHBOARD_PASSWORD` is empty, KAOS generates a temporary password at startup and logs it locally.

Capability gates:

```bash
ENABLE_DYNAMIC_TOOLS=false
REQUIRE_DYNAMIC_TOOL_SANDBOX=true
ENABLE_MCP_GATEWAY_MANAGEMENT=false
ENABLE_DYNAMIC_MCP_SERVERS=false
ENABLE_SERVER_OPS=false
```

Enable these only in trusted deployments.

## Systemd

Example single-agent unit:

```ini
[Unit]
Description=Kronos Agent OS
After=network.target

[Service]
Type=simple
User=kaos
WorkingDirectory=/opt/kaos/app
ExecStart=/opt/kaos/app/.venv/bin/python -m kronos
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/opt/kaos/app/.env

[Install]
WantedBy=multi-user.target
```

Install/update:

```bash
cd /opt/kaos/app
python3 -m venv .venv
.venv/bin/pip install -e ".[memory]"
sudo systemctl restart kaos
```

## Multi-Agent / Swarm Mode

Each agent should run as a separate process with its own:

- `AGENT_NAME`
- Telegram session file
- Telegram credentials/account where applicable
- `WEBHOOK_PORT` if using webhook notifications
- workspace at `workspaces/<agent>/`

The shared swarm ledger is configured by:

```bash
SWARM_DB_PATH=./data/swarm.db
```

For systemd-managed sub-agents, copy the template unit and create one private
env file per agent:

```bash
sudo cp systemd/kaos@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kaos@kaos-worker
```

## Server Ops

Server operations are disabled by default. To enable:

```bash
ENABLE_SERVER_OPS=true
SERVER_REGISTRY_PATH=/opt/kaos/servers.yaml
```

Use `servers.example.yaml` as the template. Keep `servers.yaml` private and gitignored.

## Health Checks

```bash
kaos doctor
curl http://127.0.0.1:8788/health
curl http://127.0.0.1:8789/api/health
```

## Data Layout

```text
/opt/kaos/
├── kronos/
├── dashboard/
├── dashboard-ui/
├── workspaces/              # local runtime state
│   └── _template/           # public starter template
├── data/
│   ├── <agent>/session.db
│   ├── <agent>/memory_fts.db
│   ├── <agent>/knowledge_graph.db
│   └── swarm.db
├── .env
├── agents.yaml
└── servers.yaml
```

`data/`, `.env`, `agents.yaml`, `servers.yaml`, `*.session`, and live `workspaces/<agent>/` files should not be committed.
