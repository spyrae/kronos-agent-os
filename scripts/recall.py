#!/usr/bin/env python3
"""FTS5 Cross-Session Search — full-text search across audit log.

Builds a SQLite FTS5 index from Kronos Agent OS audit.jsonl.
Supports search with optional LLM summarization via DeepSeek API.

Usage:
    recall.py index              — rebuild FTS5 index from audit log
    recall.py search "query"     — search and print results
    recall.py search "query" -n 5 — limit to 5 results
    recall.py search "query" --summarize — search + LLM summarize
    recall.py stats              — show index statistics

Environment:
    AUDIT_LOG           Audit log path (default: /opt/kaos/data/audit.jsonl)
    DB_PATH             SQLite database path (default: /opt/kaos/data/recall.db)
    DEEPSEEK_API_KEY    DeepSeek API key (for --summarize)
    RECALL_LOG          Log file (default: /var/log/kaos/recall.log)
"""

import json
import logging
import os
import re
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# --- Config ---

AUDIT_LOG = Path(os.environ.get("AUDIT_LOG", "/opt/kaos/data/audit.jsonl"))
DB_PATH = Path(os.environ.get("DB_PATH", "/opt/kaos/data/recall.db"))
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
LOG_FILE = os.environ.get("RECALL_LOG", "/var/log/kaos/recall.log")

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"

# --- Logging ---

log_dir = Path(LOG_FILE).parent
log_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("recall")


# --- LLM ---

def ask_deepseek(prompt: str, timeout: int = 60) -> str:
    """Call DeepSeek chat completions API. Stdlib only (urllib)."""
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")

    payload = json.dumps({
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4000,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{DEEPSEEK_BASE_URL}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        },
    )

    resp = urllib.request.urlopen(req, timeout=timeout)
    data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


# --- Database ---

def init_db() -> sqlite3.Connection:
    """Create/open the recall database with FTS5."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            source TEXT,          -- 'audit'
            role TEXT,            -- 'user', 'assistant'
            content TEXT NOT NULL,
            timestamp TEXT,
            metadata TEXT
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            content,
            role,
            source,
            content_rowid='id',
            tokenize='unicode61'
        );

        CREATE TABLE IF NOT EXISTS index_state (
            source TEXT PRIMARY KEY,
            last_file TEXT,
            last_offset INTEGER DEFAULT 0,
            updated_at TEXT
        );
    """)
    return conn


# --- Indexing ---

def extract_messages_from_audit(filepath: Path) -> list[dict]:
    """Extract messages from Kronos Agent OS audit.jsonl.

    Kronos Agent OS audit format fields:
        ts, tier, duration_ms, input_tokens, output_tokens,
        approx_cost_usd, input_preview, output_preview
    """
    messages = []
    for line in filepath.read_text(errors="replace").strip().split("\n"):
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        ts = entry.get("ts", "")
        inp = entry.get("input_preview", "").strip()
        out = entry.get("output_preview", "").strip()

        # Build optional metadata for context
        meta = {
            k: entry[k]
            for k in ("tier", "duration_ms", "input_tokens", "output_tokens", "approx_cost_usd")
            if k in entry
        }
        meta_str = json.dumps(meta, ensure_ascii=False) if meta else ""

        if inp and len(inp) >= 5:
            messages.append({
                "session_id": "audit",
                "role": "user",
                "content": inp,
                "timestamp": ts,
                "metadata": meta_str,
            })
        if out and len(out) >= 5:
            messages.append({
                "session_id": "audit",
                "role": "assistant",
                "content": out,
                "timestamp": ts,
                "metadata": meta_str,
            })

    return messages


def build_index() -> None:
    """Build/rebuild FTS5 index from audit log."""
    conn = init_db()

    # Clear existing data
    conn.execute("DELETE FROM messages")
    conn.execute("DELETE FROM messages_fts")

    total = 0

    # Index audit log
    if AUDIT_LOG.exists():
        audit_msgs = extract_messages_from_audit(AUDIT_LOG)
        for msg in audit_msgs:
            conn.execute(
                "INSERT INTO messages (session_id, source, role, content, timestamp, metadata)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("audit", "audit", msg["role"], msg["content"], msg["timestamp"], msg.get("metadata", "")),
            )
        total += len(audit_msgs)
        log.info("Indexed audit: %d messages", len(audit_msgs))
    else:
        log.warning("Audit log not found: %s", AUDIT_LOG)

    # Rebuild FTS5
    conn.execute("""
        INSERT INTO messages_fts (rowid, content, role, source)
        SELECT id, content, role, source FROM messages
    """)

    conn.execute(
        "INSERT OR REPLACE INTO index_state (source, last_file, last_offset, updated_at) VALUES (?, ?, ?, ?)",
        ("full", "rebuild", total, datetime.now(timezone.utc).isoformat()),
    )

    conn.commit()
    conn.close()

    log.info("Index built: %d total messages", total)


# --- Search ---

def search(query: str, limit: int = 10, role_filter: str = "") -> list[dict]:
    """Search FTS5 index."""
    if not DB_PATH.exists():
        log.error("Index not found. Run: recall.py index")
        return []

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Escape FTS5 special characters
    safe_query = re.sub(r'[^\w\s]', ' ', query).strip()
    if not safe_query:
        return []

    # Use FTS5 MATCH with ranking
    sql = """
        SELECT m.id, m.session_id, m.source, m.role, m.content, m.timestamp,
               messages_fts.rank
        FROM messages_fts
        JOIN messages m ON m.id = messages_fts.rowid
        WHERE messages_fts MATCH ?
    """
    params = [safe_query]

    if role_filter:
        sql += " AND m.role = ?"
        params.append(role_filter)

    sql += " ORDER BY messages_fts.rank LIMIT ?"
    params.append(limit)

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        log.error("FTS5 search failed: %s", e)
        rows = []

    results = []
    for row in rows:
        results.append({
            "session_id": row["session_id"],
            "source": row["source"],
            "role": row["role"],
            "content": row["content"],
            "timestamp": row["timestamp"],
            "rank": row["rank"],
        })

    conn.close()
    return results


def format_results(results: list[dict]) -> str:
    """Format search results for display."""
    if not results:
        return "Ничего не найдено."

    lines = [f"Найдено: {len(results)} результатов\n"]
    for i, r in enumerate(results, 1):
        ts = r["timestamp"][:16] if r["timestamp"] else "?"
        role = "👤" if r["role"] == "user" else "🤖"
        source = r["source"]
        content = r["content"][:200]
        lines.append(f"{i}. [{ts}] {role} ({source})\n   {content}\n")

    return "\n".join(lines)


# --- LLM Summarization ---

def summarize_results(query: str, results: list[dict]) -> str:
    """Summarize search results via DeepSeek API."""
    context = "\n\n".join(
        f"[{r['timestamp'][:16]}] {r['role']}: {r['content'][:500]}"
        for r in results[:10]
    )

    prompt = f"""Пользователь ищет в истории разговоров: "{query}"

Найденные фрагменты:
{context}

Дай краткую сводку: что было найдено по этому запросу? Ключевые факты и решения.
Формат: 2-5 предложений, только суть."""

    try:
        return ask_deepseek(prompt, timeout=90)
    except Exception as e:
        log.error("Summarization failed: %s", e)
        return format_results(results)


# --- Stats ---

def show_stats() -> None:
    """Show index statistics."""
    if not DB_PATH.exists():
        print("No index found. Run: recall.py index")
        return

    conn = sqlite3.connect(str(DB_PATH))
    total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    by_source = conn.execute(
        "SELECT source, COUNT(*) FROM messages GROUP BY source"
    ).fetchall()
    by_role = conn.execute(
        "SELECT role, COUNT(*) FROM messages GROUP BY role"
    ).fetchall()
    state = conn.execute("SELECT * FROM index_state").fetchall()
    conn.close()

    print(f"Total messages: {total}")
    print(f"By source: {dict(by_source)}")
    print(f"By role: {dict(by_role)}")
    print(f"DB size: {DB_PATH.stat().st_size / 1024:.1f} KB")
    for s in state:
        print(f"Last index: {s[2]} entries at {s[3]}")


# --- CLI ---

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage:")
        print("  recall.py index                    — rebuild FTS5 index")
        print("  recall.py search 'query' [-n N]    — search")
        print("  recall.py search 'query' --summarize — search + LLM summary")
        print("  recall.py stats                    — show index info")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "index":
        build_index()

    elif cmd == "search":
        if len(sys.argv) < 3:
            print("Usage: recall.py search 'query'")
            sys.exit(1)

        query = sys.argv[2]
        limit = 10
        summarize = False

        for arg in sys.argv[3:]:
            if arg == "--summarize":
                summarize = True
            elif arg == "-n" or arg.startswith("-n"):
                try:
                    idx = sys.argv.index("-n")
                    limit = int(sys.argv[idx + 1])
                except (ValueError, IndexError):
                    pass

        results = search(query, limit=limit)

        if summarize and results:
            print(summarize_results(query, results))
        else:
            print(format_results(results))

    elif cmd == "stats":
        show_stats()

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
