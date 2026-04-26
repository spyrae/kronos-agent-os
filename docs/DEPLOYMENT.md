# Kronos II — Deployment

## VPS

| Parameter | Value |
|-----------|-------|
| Host | `user@your-server-ip` |
| Path | `/opt/kronos-ii/` |
| Service | `kronos-ii.service` (systemd) |
| Python | 3.11+ |
| Ports | 8788 (webhook), 3000 (dashboard) |

## Deploy Process

Local development → scp → restart:

```bash
# 1. From local machine
scp -r kronos/ user@your-server-ip:/opt/kronos-ii/kronos/
scp pyproject.toml user@your-server-ip:/opt/kronos-ii/

# 2. On server
ssh user@your-server-ip
cd /opt/kronos-ii
pip install -e ".[dev]"
sudo systemctl restart kronos-ii

# 3. Check status
sudo systemctl status kronos-ii
journalctl -u kronos-ii -f
```

## Systemd Service

```ini
[Unit]
Description=Kronos II AI Agent
After=network.target

[Service]
Type=simple
User=kronos
WorkingDirectory=/opt/kronos-ii
ExecStart=/usr/bin/python -m kronos
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

## .env Configuration

Required environment variables (in `/opt/kronos-ii/.env`):

### LLM Providers (at least one required)
```bash
ANTHROPIC_API_KEY=sk-ant-...        # Claude Sonnet 4 (standard tier)
DEEPSEEK_API_KEY=sk-...             # DeepSeek V3 (lite tier + memory extraction)
GOOGLE_API_KEY=AIza...              # Gemini 2.0 Flash (fallback)
```

### Telegram (required)
```bash
TG_API_ID=12345678                  # Telethon app ID
TG_API_HASH=abcdef...              # Telethon app hash
TG_BOT_TOKEN=123:ABC...            # Bot API token (optional — enables bot mode)
ALLOWED_USER_IDS=                  # Comma-separated user IDs (empty = allow all)
```

### Discord (optional)
```bash
DISCORD_BOT_TOKEN=...              # Discord bot token
DISCORD_ALLOWED_GUILDS=123,456     # Comma-separated guild IDs
```

### Webhook
```bash
WEBHOOK_SECRET=random-secret-here   # Authenticates webhook requests
WEBHOOK_PORT=8788                   # Default: 8788
```

### Memory
```bash
MEM0_QDRANT_PATH=data/qdrant_data  # Qdrant local storage path
CONTEXT_STRATEGY=summarize          # summarize | sliding_window | hybrid
```

### MCP Tool Servers
```bash
BRAVE_API_KEY=...                   # Brave Search
EXA_API_KEY=...                     # Exa deep search
NOTION_API_KEY=ntn_...              # Notion integration
GOOGLE_OAUTH_CLIENT_ID=...          # Google Workspace (Gmail, Calendar)
GOOGLE_OAUTH_CLIENT_SECRET=...
```

### Voice
```bash
GROQ_API_KEY=gsk_...               # Groq Whisper STT
```

### Cron Topics
```bash
DEFAULT_NOTIFY_CHAT=               # Default chat for cron notifications
NEWS_TOPIC_ID=                     # Telegram topic for news digest
DIGEST_TOPIC_ID=                   # Telegram topic for group digest
SCOUT_TOPIC_ID=                    # Telegram topic for people scout
FINANCE_TOPIC_ID=0                 # Telegram topic for finance reports
```

### Database
```bash
DB_PATH=data/kronos.db             # SQLite checkpointer path
```

### Observability
```bash
LANGFUSE_PUBLIC_KEY=...            # Langfuse tracing (optional)
LANGFUSE_SECRET_KEY=...
LANGFUSE_HOST=...
```

## MCP Servers

11 static MCP servers configured in `kronos/tools/mcp_servers.py`. Each requires specific env vars:

| Server | Required Env | Binary |
|--------|-------------|--------|
| brave-search | `BRAVE_API_KEY` | npx @brave/brave-search-mcp-server |
| exa | `EXA_API_KEY` | npx exa-mcp-server |
| fetch | — | uvx mcp-server-fetch |
| content-core | — | uvx content-core-mcp |
| reddit | — | npx reddit-mcp-buddy |
| notion | `NOTION_API_KEY` | npx @notionhq/notion-mcp-server |
| google-workspace | `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` | uvx workspace-mcp |
| youtube | — | npx @kimtaeyoon83/mcp-server-youtube-transcript |
| markitdown | — | uvx markitdown-mcp |
| yahoo-finance | — | uvx mcp-yahoo-finance |
| filesystem | — | npx @modelcontextprotocol/server-filesystem |

**Dependencies:** `npx` (Node.js) and `uvx` (uv) must be installed on the server.

## Data Directory

```
/opt/kronos-ii/
├── kronos/                ← Source code
├── workspace/             ← Persona, skills, dynamic tools
├── data/                  ← Runtime data (gitignored)
│   ├── kronos.db
│   ├── memory_fts.db
│   ├── knowledge_graph.db
│   ├── mcp_registry.db
│   ├── qdrant_data/
│   └── logs/
│       └── audit.jsonl
├── .env                   ← Secrets (gitignored)
├── pyproject.toml
└── kronos-ii.session      ← Telethon session file
```

## Health Check

```bash
# Service status
sudo systemctl status kronos-ii

# Logs (follow)
journalctl -u kronos-ii -f

# Health endpoint
curl http://localhost:8788/health
# {"status": "ok", "agent": "kronos-ii"}

# Webhook test
curl -X POST http://localhost:8788/webhook \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
  -d '{"text": "Test message"}'
```

## Workspace Backup

Automated backup via cron (every 6 hours):
- rsync workspace → backup directory
- git commit + push to backup repo
- NTFY notification on success/failure
