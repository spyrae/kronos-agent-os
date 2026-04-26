# Kronos II — Memory System

4-layer memory architecture providing conversation persistence, semantic search, knowledge graph, and nightly consolidation.

## Layer Overview

| Layer | Storage | Purpose | Cost | Speed |
|-------|---------|---------|------|-------|
| **L1** Checkpointer | SQLite (`kronos.db`) | Conversation state per thread | $0 | Instant |
| **L2** Hybrid Search | Qdrant (vectors) + SQLite FTS5 (keywords) | Semantic + keyword fact retrieval | ~$0.001/query (DeepSeek extraction) | ~200ms |
| **L3** Knowledge Graph | SQLite (`knowledge_graph.db`) | Entity relationships | $0 (query) | ~10ms |
| **L4** Sleep Compute | Cron job (daily 03:00 UTC) | Nightly consolidation, dedup, insights | ~$0.01/run | Background |

## L1: LangGraph Checkpointer

**Implementation:** `AsyncSqliteSaver` from `langgraph.checkpoint.sqlite.aio`
**Storage:** `data/kronos.db`

Stores full conversation state (all messages, metadata) per thread. Enables conversation continuity across restarts.

### Thread Isolation

| Channel | Thread ID Format | Example |
|---------|-----------------|---------|
| Telegram DM | `{chat_id}` | `123456789` |
| Telegram Topic | `{chat_id}:{topic_id}` | `-1001234:5678` |
| Discord Channel | `discord:{channel_id}` | `discord:987654` |
| Discord Thread | `discord:{channel_id}:{thread_id}` | `discord:987654:111222` |

### Context Clearing
`/clear` or `/reset` command deletes checkpoints and writes for the current thread from SQLite directly.

## L2: Mem0 + FTS5 Hybrid Search

Two parallel indexes for the same facts, merged for best results.

### Mem0 (Vector Search)

**Implementation:** `kronos/memory/store.py`
**Config:**
- **LLM:** DeepSeek V3 for fact extraction ($0.27/1M tokens, temperature=0.2)
- **Embedder:** HuggingFace `multi-qa-MiniLM-L6-cos-v1` (384 dimensions, local, free)
- **Vector Store:** Qdrant local mode (in-process, no server) at `data/qdrant_data/`

**Strengths:** Semantic similarity — finds related concepts even with different vocabulary.
**Weaknesses:** Misses exact matches (names, IDs, dates, numbers).

### FTS5 (Keyword Search)

**Implementation:** `kronos/memory/fts.py`
**Storage:** `data/memory_fts.db`
**Schema:**
```sql
-- Raw facts
CREATE TABLE memory_facts (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT DEFAULT 'mem0',
    created_at TEXT NOT NULL,
    mem0_id TEXT
);

-- FTS5 virtual table
CREATE VIRTUAL TABLE memory_fts USING fts5(
    content,
    user_id UNINDEXED,
    tokenize='unicode61'
);
```

**Query sanitization:** Strips FTS5 operators, quotes each token, caps at 10 tokens.

**Strengths:** Exact keyword matching — perfect for names, dates, IDs, URLs.
**Weaknesses:** No semantic understanding.

### Hybrid Merge (`kronos/memory/hybrid.py`)

Both searches run in parallel, then results are merged:

**1. Score Normalization**
- Vector scores: already 0-1 (cosine similarity)
- FTS5 ranks: negative BM25 → normalized to 0-1 by dividing by max rank

**2. Weighted Combination**
```
hybrid_score = vector_score * 0.7 + fts_score * 0.3
```

**3. Agreement Boost**
Facts found by BOTH methods get 20% boost:
```
if vector_score > 0 and fts_score > 0:
    hybrid_score *= 1.2
```

**4. Temporal Decay**
Score decays with age using half-life of 60 days:
```
decay = 2^(-age_days / 60)
hybrid_score *= decay
```

**5. MMR Re-ranking**
Maximal Marginal Relevance (λ=0.7) balances relevance vs diversity:
- Greedy selection: each step picks candidate maximizing `λ * relevance - (1-λ) * max_similarity_to_selected`
- Similarity = word overlap (Jaccard-like), no extra embeddings needed
- Prevents returning 5 nearly-identical memories

### Storage Pipeline

When agent responds (`store_memories_background` node):

1. Extract last user-assistant turn
2. **Mem0 fact extraction** → stores in Qdrant + returns extracted facts
3. **FTS5 batch indexing** of extracted facts (with deduplication)
4. **FTS5 raw indexing** of full conversation turn (catches phrases Mem0 misses)

### Retrieval Pipeline

Before LLM call (`retrieve_memories` node):

1. Get last user message
2. **Hybrid search** (vector + FTS5, limit=5 results)
3. **Knowledge Graph context** (entity connections, limit=3)
4. Inject as `SystemMessage`:
```
[Relevant memories]
- fact 1
- fact 2

[Knowledge graph]
Alice (person): works_at→Acme Corp, uses→Python
```

## L3: Knowledge Graph

**Implementation:** `kronos/memory/knowledge_graph.py`
**Storage:** `data/knowledge_graph.db`

### Schema

```sql
CREATE TABLE entities (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,         -- person|company|project|concept|tool|location|event
    properties TEXT DEFAULT '{}', -- JSON
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE relations (
    id INTEGER PRIMARY KEY,
    source_id INTEGER REFERENCES entities(id),
    target_id INTEGER REFERENCES entities(id),
    relation_type TEXT NOT NULL, -- knows|works_at|uses|owns|related_to|part_of|created
    properties TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);
```

### Entity Types
`person`, `company`, `project`, `concept`, `tool`, `location`, `event`

### Relation Types
`knows`, `works_at`, `uses`, `owns`, `related_to`, `part_of`, `created`

### Operations
- `add_entity(name, type, properties)` — upsert by name+type
- `add_relation(source, target, relation_type)` — creates entities if needed, upsert relation
- `get_connections(entity_name, depth=1)` — all direct connections (outgoing + incoming)
- `search_entities(query, type)` — LIKE search by name
- `get_graph_context(query, limit)` — formatted string for LLM injection

### Integration
- **Retrieval:** `retrieve_memories` node queries graph for entities matching user message
- **Population:** Sleep-time Compute cron extracts entities from recent facts

## L4: Sleep-time Compute

**Implementation:** `kronos/cron/sleep_compute.py`
**Schedule:** Daily 03:00 UTC (11:00 UTC+8 — while user sleeps)

### Pipeline

**Step 1: Collect Recent Facts**
- Query FTS5 for facts from last 7 days (up to 100)
- Skip if < 5 facts

**Step 2: Entity Extraction**
- LLM (DeepSeek lite) analyzes facts
- Outputs JSON with entities and relations
- Rules: only clearly stated facts, normalize names, skip vague references
- Results added to Knowledge Graph via `add_entity()` and `add_relation()`

**Step 3: Insight Generation**
- Query Knowledge Graph for recent entities and connections
- LLM generates 1-3 actionable insights from patterns
- Example: "User has been discussing Project Alpha and Project Beta frequently — both in active development"

**Step 4: Stale Cleanup**
- Delete FTS5 facts older than 90 days
- Both `memory_facts` table and `memory_fts` virtual table cleaned

**Step 5: Report**
```
🌙 Sleep Compute завершён
Entities: +5 (total: 47)
Relations: +3 (total: 82)
Cleaned: 12 stale facts

💡 Инсайты:
• ...
```

## Context Engine (`kronos/memory/context_engine.py`)

Pluggable strategies for managing conversation context window. Selected via `CONTEXT_STRATEGY` setting.

### Strategies

#### 1. Summarize (default)
- **Trigger:** message count > 30
- **Action:** LLM summarizes old messages (keeping last 6), preserving critical identifiers
- **Preservation:** UUIDs, URLs, IPs, file paths, batch progress, TODO items, names, dates, amounts
- **Cost:** ~$0.01 per compaction (DeepSeek lite)
- **Chunk support:** Long conversations split into 6000-char chunks, summarized individually, then merged

#### 2. Sliding Window
- **Trigger:** message count > 20
- **Action:** Drop oldest messages, keep last 20
- **Cost:** $0 (no LLM calls)
- **Best for:** Casual conversations, low-cost mode

#### 3. Hybrid
- **Trigger:** message count > 30
- **Action:** Keep last 24 messages, flush dropped messages to Mem0 long-term memory
- **Cost:** ~$0.005 per flush (Mem0 extraction only, no summarization)
- **Best for:** Balanced cost/quality, personal assistant use case
- Adds marker: `[Context window: N older messages moved to long-term memory]`

### Compaction with Identity Preservation (`kronos/memory/compaction.py`)

The summarize strategy uses a specialized prompt that preserves:
1. **Context** — what the conversation was about
2. **Decisions** — what was decided and why
3. **Progress** — what's done, what's in progress
4. **Pending** — what remains unfinished
5. **Data** — all IDs, URLs, paths, numbers from conversation

Critical data that MUST be preserved verbatim (not paraphrased):
- UUIDs, hashes, tokens, commit IDs
- URLs, hostnames, IP addresses, file paths
- Batch operation progress ("processed 5/17 items")
- Active task status and decisions with reasoning
- API keys shown masked (sk-...abc)

Max 600 words per summary. Facts flushed to Mem0 + FTS5 before being dropped.

## Data Files

```
data/
├── kronos.db                    ← L1: conversation checkpoints
├── memory_fts.db                ← L2: FTS5 keyword index
├── knowledge_graph.db           ← L3: entities and relations
├── qdrant_data/                 ← L2: Mem0 vector embeddings
│   └── kronos_memories/         ←     Qdrant collection
└── logs/
    └── audit.jsonl              ← Used by cron jobs for analysis

workspace/
├── memory/
│   └── self-improve/            ← Self-improvement learning records
│       └── YYYY-MM-DD.md
├── MEMORY.md                    ← Static long-term facts (loaded into system prompt)
├── USER-MODEL.md                ← Dialectical user model (updated weekly)
└── USER-PATTERNS.md             ← Quantitative user patterns (updated weekly)
```
