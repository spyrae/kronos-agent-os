# Changelog

## [0.1.0] - 2026-04-26

Initial open source release.

### Core
- Custom ReAct engine replacing LangGraph (`engine.py`)
- KronosAgent pipeline: validate > memory > route > store
- Pydantic Settings configuration

### Swarm Coordination
- SQLite-based claim arbitration (no Redis/pub-sub)
- Tier 1/2/3 routing with cross-agent addressing guard
- Shared user facts via FTS5
- Swarm metrics tracking

### Memory
- Mem0 + Qdrant (local mode) for vector search
- FTS5 keyword search with hybrid merge
- Knowledge graph (entities + relations)
- Sleep-time compute (nightly consolidation)
- Pluggable context engine (summarize / sliding window / hybrid)

### Transport
- Telethon userbot bridge with rate limiting and typing simulation
- Discord bridge (experimental)
- Webhook server for cron integration

### Agents
- Supervisor with sub-agent routing (research, task, finance)
- Deep research pipeline
- Topic research pipeline
- Competitor monitoring
- Telegram channels agent

### Security
- Prompt injection shield (28 patterns, EN+RU)
- Output validator
- Cost guardian
- Loop detector

### Ops
- 18 built-in cron jobs
- NTFY push notifications
- Server ops tools (SSH-based diagnostics)
- Health check, daily status, workspace backup scripts
- Systemd units for all 6 agents

### Persona
- Three-Space workspace architecture (self / notes / ops)
- 6 included agent personas
- Workspace template for new agents
- Configurable agent profiles via `agents.yaml`
