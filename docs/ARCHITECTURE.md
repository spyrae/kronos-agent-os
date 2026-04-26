# Kronos II — Architecture

## System Diagram

```
                         ┌─────────────────────────────────────────────┐
                         │              Channels                       │
                         │                                             │
                         │  Telegram        Discord       Webhook      │
                         │  (Telethon)    (discord.py)   (aiohttp)     │
                         │  DM / Groups   DM / Mentions  POST /webhook │
                         │  Forum Topics  Threads         + cron jobs  │
                         └────────┬──────────┬──────────────┬──────────┘
                                  │          │              │
                                  ▼          ▼              ▼
                         ┌─────────────────────────────────────────────┐
                         │           Application Layer (app.py)        │
                         │                                             │
                         │  AsyncSqliteSaver (L1 Checkpointer)         │
                         │  Scheduler (11 cron jobs)                   │
                         │  Dashboard (web UI)                         │
                         └──────────────────┬──────────────────────────┘
                                            │
                                            ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│                         LangGraph StateGraph                                  │
│                                                                               │
│  validate ──┬── retrieve_memories ── call_model ──┬── store_memories ──┬── END│
│             │                         ↕           │                   │       │
│             │                    call_tools        │               compact    │
│             │                   (ReAct loop)       │                          │
│             └─── END (rejected)                    └── END (no compact)       │
│                                                                               │
│  Supervisor Mode (when tools available):                                      │
│  call_model = Supervisor Graph ─┬── research_agent                            │
│                                 ├── deep_research_agent                       │
│                                 ├── topic_research_agent                      │
│                                 ├── task_agent                                │
│                                 ├── finance_agent                             │
│                                 └── direct tools (skills, gateway, dynamic)   │
└───────────────────────────────────────────────────────────────────────────────┘
                         │                              │
                         ▼                              ▼
              ┌────────────────────┐         ┌────────────────────┐
              │   Memory System    │         │     Tool System     │
              │                    │         │                     │
              │ L1: SQLite CP      │         │ MCP Gateway         │
              │ L2: Mem0 + FTS5    │         │ ├── 11 static       │
              │ L3: Knowledge Graph│         │ └── N dynamic       │
              │ L4: Sleep Compute  │         │                     │
              │                    │         │ Browser (Playwright) │
              │ Context Engine     │         │ Dynamic Tools        │
              │ ├── summarize      │         │ Composio             │
              │ ├── sliding_window │         │ Skill Tools          │
              │ └── hybrid         │         └────────────────────┘
              └────────────────────┘
```

## Graph Flow

The main LangGraph StateGraph processes every message through this pipeline:

### 1. validate
Input validation via prompt injection shield (28 regex patterns) + rate limiting (10 req/min per user).
- **Pass** → `retrieve_memories`
- **Blocked** → returns rejection message → `END`

### 2. retrieve_memories
Hybrid search (vector + keyword) for relevant context:
- Mem0/Qdrant vector search (semantic similarity)
- FTS5 keyword search (exact names, dates, IDs)
- Knowledge Graph context (entity connections)
- Results injected as `SystemMessage` before LLM call

### 3. call_model
Two modes depending on available tools:

**Supervisor Mode** (default when tools available):
- `langgraph-supervisor` routes to specialized sub-agents
- Supervisor handles skills, gateway, and dynamic tools directly
- Sub-agents: research, deep_research, topic_research, task, finance

**Single-Agent Mode** (fallback when no tools):
- Direct LLM call with persona system prompt
- LLM tier routing: lite (<30 chars, simple) → DeepSeek; standard → Sonnet
- Automatic fallback chain on provider failure

### 4. call_tools (Single-Agent Mode only)
ReAct loop: model requests tool calls → ToolNode executes → back to model.
- Loop detector monitors for stuck agents (warning → critical → circuit breaker)
- Continues until model responds without tool calls

### 5. store_memories
Background fact extraction and storage:
- Mem0 extracts facts from conversation turn (DeepSeek LLM)
- Facts indexed in both Qdrant (vector) and FTS5 (keyword)
- Raw conversation turn also indexed in FTS5

### 6. compact (conditional)
Triggered when message count exceeds threshold (default: 30):
- Strategy determined by Context Engine (summarize/sliding_window/hybrid)
- Summarization preserves critical identifiers (UUIDs, URLs, progress)
- Dropped messages flushed to long-term memory

## Supervisor Pattern

The supervisor (`langgraph-supervisor`) is the central routing agent:

```
User Message → Supervisor (Sonnet)
                    │
                    ├── Skill match? → load_skill() → follow protocol
                    ├── Deep research? → deep_research_agent (multi-step pipeline)
                    ├── Blog topics? → topic_research_agent
                    ├── Quick web search? → research_agent
                    ├── Tasks/calendar/email? → task_agent
                    ├── Finance/stocks? → finance_agent
                    └── Simple/conversational → respond directly
```

### Sub-Agents

| Agent | LLM Tier | Tools | Purpose |
|-------|----------|-------|---------|
| `research_agent` | Standard (Sonnet) | brave, exa, fetch, content-core, reddit | Quick web search, content extraction |
| `deep_research_agent` | Standard | brave, exa, fetch, content-core | Multi-step research: plan → search → evaluate → synthesize |
| `topic_research_agent` | Standard | brave, exa, fetch, content-core | Blog topic discovery and validation |
| `task_agent` | Lite (DeepSeek) | notion, google-workspace, filesystem | Notion, calendar, email, files |
| `finance_agent` | Standard | yahoo-finance, brave | Stock prices, financial analysis |

### Supervisor-Only Tools
These tools are called directly by the supervisor, not delegated:
- `load_skill`, `load_skill_reference` — skill system
- `mcp_add_server`, `mcp_remove_server`, `mcp_list_servers`, `mcp_reload` — gateway management
- `create_new_tool`, `list_dynamic_tools` — dynamic tool creation

## Memory 4-Layer System

### L1: LangGraph Checkpointer (SQLite)
- `AsyncSqliteSaver` stores full conversation state per thread
- Thread isolation: `chat_id` for DM, `chat_id:topic_id` for forum topics, `discord:channel_id:thread_id` for Discord
- Conversation persists across restarts via `data/kronos.db`

### L2: Mem0 (Vector) + FTS5 (Keyword) → Hybrid Search
**Mem0 (Qdrant vectors):**
- DeepSeek extracts facts from conversations ($0.27/1M tokens)
- HuggingFace `multi-qa-MiniLM-L6-cos-v1` embeddings (384 dims, local, free)
- Qdrant local mode (in-process, no external server)
- Good for: semantic similarity, finding related concepts

**FTS5 (SQLite keyword index):**
- Same facts indexed for exact keyword matching
- Good for: names, dates, numbers, IDs, URLs — things vector search misses
- `unicode61` tokenizer, BM25 ranking

**Hybrid Merge:**
- Both searches run in parallel
- Score normalization: vector scores (0-1 cosine), FTS5 BM25 → 0-1 scale
- Weighted merge: 70% vector + 30% keyword
- 20% boost for facts found by both methods
- Temporal decay: half-life 60 days (`score * 2^(-age/60)`)
- MMR re-ranking for diversity (λ=0.7)

### L3: Knowledge Graph (SQLite)
- Entities: person, company, project, concept, tool, location, event
- Relations: knows, works_at, uses, owns, related_to, part_of, created
- Queried during memory retrieval (`get_graph_context`)
- Built incrementally from conversations via Sleep-time Compute

### L4: Sleep-time Compute (Nightly Cron)
Runs daily at 03:00 UTC (11:00 UTC+8):
1. Deduplicate similar facts in FTS5
2. LLM extracts entities from recent facts → Knowledge Graph
3. Build/update relations between entities
4. Generate insights from graph patterns
5. Clean up stale memories (>90 days)

## Skill System (Progressive Disclosure)

Three levels of information loading to minimize token usage:

### L1: Catalog (always in system prompt)
- `~50-100 tokens per skill`
- Format: `- **skill-name**: description [refs: REF1, REF2]`
- Built by `SkillStore.build_catalog()`
- Injected via `build_system_prompt()` into both main agent and supervisor

### L2: Full Protocol (loaded via tool call)
- `load_skill(skill_name)` → returns full SKILL.md content
- Contains: triggers, pipeline steps, templates, rules
- LLM loads when it recognizes a matching user request

### L3: References (loaded on demand)
- `load_skill_reference(skill_name, ref_name)` → returns reference file
- Contains: WATCHLIST.md, CRITERIA.md, BUDGET.md, GROUPS.md, etc.
- Loaded only when skill protocol instructs to

### Skill Discovery
- Skills stored in `workspace/skills/{name}/SKILL.md`
- YAML frontmatter: `name`, `description`
- Optional `references/` directory with `.md` files
- `SkillStore` scans on startup, builds catalog

## Security 5-Layer System

### Layer 1: Input Validation (`shield.py`)
- 28 regex patterns: instruction override, role manipulation, system prompt extraction, credential extraction, encoding tricks
- Bilingual: English + Russian injection patterns
- Rate limiter: 10 requests per 60 seconds per user
- Blocks → returns "Запрос заблокирован системой безопасности."

### Layer 2: Sanitization (`sanitize.py`)
- Unicode homoglyph folding (NFKC normalization): fullwidth → ASCII
- Cyrillic lookalike detection (mixed-script attacks)
- HTML hidden element stripping (display:none, visibility:hidden, zero-size, white-on-white)
- Boundary markers with cryptographic random IDs (`wrap_untrusted`)
- Control character stripping, line truncation (>2000 chars)

### Layer 3: Loop Detection (`loop_detector.py`)
- Monitors ReAct tool call loop in `should_continue_after_model`
- Three detectors: generic_repeat, ping_pong, poll_no_progress
- Escalation: WARNING (10 calls) → CRITICAL (20) → CIRCUIT_BREAKER (30)
- WARNING/CRITICAL inject nudge messages; CIRCUIT_BREAKER aborts loop

### Layer 4: Output Validation (`output_validator.py`)
- Regex-only post-processing (no LLM cost)
- Secret detection: API keys (sk-, xai-, AIza), GitHub PATs, JWTs, connection strings
- System info detection: home paths, .env references, stack traces
- Prompt leakage detection: IDENTITY.md, SOUL.md, "system prompt"
- Secrets are redacted in output; system info and prompt leaks are logged

### Layer 5: Cost Guardian (`cost_guardian.py`)
- Daily limit: $5.00 USD (blocks requests when exceeded)
- Session limit: $1.00 USD per conversation
- Warning at 80% of daily limit
- Tracks costs from audit log
- Reset: daily at midnight UTC, session on new conversation

## Channel Architecture

### Telegram (Primary)
- **Telethon userbot** or **Bot API** mode (configurable via `TG_BOT_TOKEN`)
- DM: responds to allowed users (`ALLOWED_USER_IDS`)
- Groups: responds to mentions and replies to bot
- Forum Topics: full topic isolation (separate thread_id per topic)
- Voice messages: Groq Whisper STT → text → agent → TTS response
- Typing indicator during processing
- Human typing delay simulation (40-80 chars/sec)
- Rate limiting: 2s per chat, 1s global
- Message chunking at 4000 chars

### Discord (Secondary)
- `discord.py` client with `message_content` intent
- Responds to mentions and DMs
- Thread isolation: `discord:{channel_id}:{thread_id}`
- Guild whitelist via `DISCORD_ALLOWED_GUILDS`
- Message chunking at 2000 chars

### Webhook API
- `POST /webhook` — send message to Telegram (authenticated via `X-Webhook-Secret`)
- `GET /history` — fetch chat history via Telethon
- `GET /health` — health check
- Port: `WEBHOOK_PORT` (default 8788)
- Used by cron jobs to send notifications

## MCP Tool Integration

### Static Servers (mcp_servers.py)
Configured at startup, require API keys:

| Category | Server | Transport | Purpose |
|----------|--------|-----------|---------|
| Search | brave-search | stdio/npx | Web search |
| Search | exa | stdio/npx | Deep semantic search |
| Search | reddit | stdio/npx | Reddit posts |
| Web | fetch | stdio/uvx | HTTP content extraction |
| Web | content-core | stdio/uvx | Page content extraction |
| Productivity | notion | stdio/npx | Task management |
| Productivity | google-workspace | stdio/uvx | Gmail, Calendar, Drive |
| Media | youtube | stdio/npx | Video transcripts |
| Media | markitdown | stdio/uvx | Document conversion |
| Finance | yahoo-finance | stdio/uvx | Stock prices, financials |
| Filesystem | filesystem | stdio/npx | Local file access |

### Dynamic Servers (MCP Gateway)
- `MCPGateway` manages server lifecycle with hot-reload
- Registry persisted in SQLite (`mcp_registry.db`)
- Add/remove servers via agent tools (`mcp_add_server`, `mcp_remove_server`)
- `mcp_reload` refreshes all tool connections

### Browser Tools (Playwright)
- Headless Chromium via Playwright (optional dependency)
- Tools: navigate, snapshot (a11y tree), screenshot, click, type, evaluate JS
- URL safety validation (blocks internal IPs, file:// protocol)
- Lazy initialization, auto-cleanup

### Dynamic Tools
- Agent creates tools via natural language description
- LLM generates Python code → validated for safety (forbidden patterns, import allowlist)
- Persisted in `workspace/dynamic_tools/*.py`
- Loaded on next startup

## LLM Configuration

### Provider Chain
```
Standard: Anthropic (claude-sonnet-4) → DeepSeek V3 → Gemini 2.0 Flash
Lite:     DeepSeek V3 → Gemini 2.0 Flash → Anthropic
```

### Tier Routing (`router.py`)
- `< 30 chars` → LITE
- Complex patterns (анализ, сравни, research, etc.) → STANDARD
- `< 100 chars` + simple patterns (привет, спасибо) → LITE
- `< 50 chars` → LITE
- Otherwise → STANDARD

### Fallback & Cooldown
- On provider error: automatic next-in-chain
- Failed providers enter 5-minute cooldown
- Last resort: ignore cooldowns, try anything
- `invoke_with_fallback()` for explicit multi-provider invocation

## Data Persistence

```
data/
├── kronos.db                 ← L1 checkpointer (conversations)
├── memory_fts.db             ← L2 FTS5 keyword index
├── knowledge_graph.db        ← L3 entity/relation store
├── mcp_registry.db           ← dynamic MCP server registry
├── qdrant_data/              ← L2 Mem0 vector storage
└── logs/
    └── audit.jsonl           ← request audit log
```

## Startup Sequence (`app.py`)

```python
1. _ensure_data_dirs()           # Create data/logs directories
2. managed_mcp_tools()           # Start all MCP servers, get tools
3. build_graph(tools)            # Build StateGraph with:
   3a. SkillStore initialization  #   - Skill catalog + tools
   3b. Browser tools              #   - Playwright (if installed)
   3c. Gateway tools              #   - MCP management tools
   3d. Dynamic tools              #   - Persisted + management tools
   3e. Composio tools             #   - Composio integration (if configured)
   3f. Context engine             #   - From CONTEXT_STRATEGY setting
   3g. Supervisor                 #   - Multi-agent routing (if tools available)
4. AsyncSqliteSaver              # Initialize conversation persistence
5. graph.compile(checkpointer)   # Compile the graph
6. setup_cron_jobs(scheduler)    # Register 11 cron jobs
7. asyncio.gather(               # Run all services concurrently:
     run_bridge(graph),          #   - Telegram bridge
     run_discord(graph),         #   - Discord bridge
     scheduler.run(),            #   - Cron scheduler
     run_dashboard(...)          #   - Web dashboard
   )
```
