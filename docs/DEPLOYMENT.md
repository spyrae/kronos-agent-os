# Kronos Agent OS (KAOS) Deployment

KAOS is local-first. Start with CLI/demo mode, then add Telegram, dashboard, MCP, cron jobs, and optional swarm processes as needed.

## Local Runtime

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,memory]"

kaos demo
cp .env.example .env
# edit .env: add one real LLM key, or configure Ollama/local
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

## GitHub Actions Deploy

Production deploys can run through a self-hosted GitHub Actions runner on the
target server. The runner should be registered to the repository with the label
`kaos-deploy`.

Register the runner on the target server, then configure it (values are
install-specific):

```text
host:        <user>@<your-server>
runner:      <your-runner-name>
labels:      self-hosted, kaos-deploy, linux, x64
deploy root: <install-dir> (passed as KAOS_REMOTE_DIR)
```

Use **Actions -> Deploy -> Run workflow**.

Inputs:

- `first_run=true`: create the target `app` dir, Python venv, and systemd
  units when `KAOS_MANAGE_SYSTEMD=true`.
- `first_run=false`: sync code, reinstall package, restart services, and verify
  `systemctl is-active`.
- `agents`: space-separated systemd services to restart and check, default
  `kaos`.
- `health_url`: optional required HTTP health check after restart.

The workflow runs `scripts/deploy.sh` with `KAOS_DEPLOY_MODE=local`, so no SSH
private key is needed in GitHub. Runtime state is preserved: `.env`, `.env.*`,
`data/`, `workspaces/`, `.venv/`, and `*.session` are excluded from rsync.

Set the workflow `remote_dir` input to the install root on the target host. The
deploy script receives it as `KAOS_REMOTE_DIR` and syncs code into
`$KAOS_REMOTE_DIR/app`.

For renamed installs, set `KAOS_SERVICES` to the real systemd unit names and
`KAOS_MAIN_UNIT` to the main agent service. Deploy rewrites ops unit
dependencies from the public template `After=kaos.service` to
`After=$KAOS_MAIN_UNIT`. The generic `kaos.service` template is installed only
when `KAOS_SERVICES` contains `kaos`; otherwise hand-managed/renamed main units
are left untouched. Set `KAOS_MANAGE_SYSTEMD=false` to skip all systemd unit
installation.

`KAOS_REMOTE_DIR` must be an absolute path using only `[A-Za-z0-9/_.-]`
(for example `/srv/kaos`) because deploy rewrites systemd templates with
that path before installation.

After every deploy, the workflow checks service state. If deploy fails, it prints
`systemctl status`, recent `journalctl` logs, disk, and memory diagnostics in the
GitHub Actions run.

## Environment

At least one LLM provider is required for chat/runtime use:

```bash
KAOS_STANDARD_PROVIDER_CHAIN=kimi,deepseek
KAOS_LITE_PROVIDER_CHAIN=deepseek,kimi
FIREWORKS_API_KEY=fw_...
DEEPSEEK_API_KEY=sk-...
```

You can use OpenAI, OpenRouter, Groq, Together, LiteLLM, Ollama, or another
OpenAI-compatible endpoint without code changes. See
[LLM Providers](LLM_PROVIDERS.md) for copy-paste recipes.

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

The files in `systemd/` are public templates. They intentionally contain
`/opt/kaos` and `User=kronos` placeholders so `scripts/deploy.sh` can rewrite
them to the target install dir and runtime user before installation. Do not
copy those templates raw on non-`/opt/kaos` installs.

Recommended install/update flow:

```bash
export KAOS_DEPLOY_MODE=local
export KAOS_REMOTE_DIR=<install-dir>
export KAOS_AGENTS="kaos"
export KAOS_SERVICES="kaos"
export KAOS_MAIN_UNIT="kaos"
bash scripts/deploy.sh --first-run
```

For a remote host, put `KAOS_DEPLOY_MODE=remote`, `KAOS_REMOTE=<user>@<host>`,
and `KAOS_REMOTE_DIR=<install-dir>` in `.env`, then run:

```bash
bash scripts/deploy.sh --first-run
```

If systemd units are provisioned by another tool (Ansible, Nix, Terraform, or
hand-managed renamed units), set `KAOS_MANAGE_SYSTEMD=false`. Deploy will still
sync code and install Python dependencies, but it will not install templates.

Equivalent single-agent unit after rewrite:

```ini
[Unit]
Description=Kronos Agent OS
After=network.target

[Service]
Type=simple
User=kaos
WorkingDirectory=<install-dir>/app
ExecStart=<install-dir>/app/.venv/bin/python -m kronos
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=<install-dir>/app/.env

[Install]
WantedBy=multi-user.target
```

Manual installs must rewrite both the install dir and runtime user before
copying units to `/etc/systemd/system`. The deploy script already does this and
also rewrites ops dependencies such as `After=kaos.service` to
`After=$KAOS_MAIN_UNIT`.

Manual package update when units are already managed elsewhere:

```bash
cd <install-dir>/app
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

For systemd-managed sub-agents, create one private env file per agent and let
deploy install/rewrite the template units:

```bash
export KAOS_MANAGE_SYSTEMD=true
bash scripts/deploy.sh --first-run
sudo systemctl enable --now kaos@kaos-worker
```

## Server Ops

Server operations are disabled by default. To enable:

```bash
ENABLE_SERVER_OPS=true
SERVER_REGISTRY_PATH=<install-dir>/app/servers.yaml
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
<install-dir>/
└── app/
    ├── kronos/
    ├── dashboard/
    ├── dashboard-ui/
    ├── workspaces/          # local runtime state
    │   └── _template/       # public starter template
    ├── data/
    │   ├── <agent>/session.db
    │   ├── <agent>/memory_fts.db
    │   ├── <agent>/knowledge_graph.db
    │   └── swarm.db
    ├── .env
    ├── agents.yaml
    └── servers.yaml
```

Default in-repo/local layout:

```text
app/
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
