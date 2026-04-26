"""Auto-compaction — manages context window by summarizing long conversations.

When message count exceeds threshold, summarizes older messages into a
compact summary and flushes key facts to Mem0 long-term memory.
"""

import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from kronos.llm import ModelTier, get_model
from kronos.memory.store import add_memories
from kronos.state import AgentState

log = logging.getLogger("kronos.memory.compaction")

# Compaction triggers when message count exceeds this
MAX_MESSAGES = 30
# Keep this many recent messages after compaction
KEEP_RECENT = 6

SUMMARIZE_PROMPT = """Сжать историю разговора ниже в краткую сводку на русском языке.

CRITICAL — сохранить дословно (НЕ перефразировать, НЕ опускать):
- UUID, хеши, токены, ID (например: a7f3b2c1-..., commit abc123)
- URL, hostnames, IP-адреса, пути к файлам
- Прогресс batch-операций (например: "обработано 5/17 элементов")
- Статус активных задач и принятых решений с обоснованием
- TODO, pending items, незавершённые действия
- Имена люде��, названия проектов, конкретные даты и сумм��
- API-ключи показывать замаскированными (sk-...abc)

Структура сводки:
1. **Контекст** — о чём шёл разговор (1-2 предложения)
2. **Решения** — что решили и почему
3. **Прогресс** — что сделано, что в процессе
4. **Pending** — что осталось незавершённым
5. **Данные** — все ID, URL, пути, числа из разговора

Максимум 600 слов. Не добавляй ничего от с��бя — только факты из разговора.

Разговор:
{conversation}"""

# Max chars per chunk for summarization (avoid exceeding LLM context)
CHUNK_SIZE = 6000


def should_compact(state: AgentState) -> bool:
    """Check if conversation needs compaction."""
    msg_count = len(state.get("messages", []))
    return msg_count > MAX_MESSAGES


def _build_conversation_text(messages: list) -> str:
    """Extract conversation text from messages."""
    parts = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            parts.append(f"User: {content}")
        elif isinstance(msg, AIMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            if content:
                parts.append(f"Kronos: {content}")
    return "\n".join(parts)


def _summarize_text(text: str, model) -> str:
    """Summarize a single chunk of conversation text."""
    prompt = SUMMARIZE_PROMPT.format(conversation=text)
    response = model.invoke([HumanMessage(content=prompt)])
    return response.content if isinstance(response.content, str) else str(response.content)


def _chunk_summarize(conversation_text: str) -> str:
    """Chunk-based summarization for long conversations.

    Splits into CHUNK_SIZE pieces, summarizes each, then merges.
    """
    model = get_model(ModelTier.LITE)

    if len(conversation_text) <= CHUNK_SIZE:
        return _summarize_text(conversation_text, model)

    # Split into chunks at line boundaries
    chunks = []
    current = ""
    for line in conversation_text.split("\n"):
        if len(current) + len(line) > CHUNK_SIZE and current:
            chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)

    log.info("Chunk summarization: %d chunks from %d chars", len(chunks), len(conversation_text))

    # Summarize each chunk
    chunk_summaries = []
    for i, chunk in enumerate(chunks):
        try:
            summary = _summarize_text(chunk, model)
            chunk_summaries.append(summary)
        except Exception as e:
            log.error("Chunk %d summarization failed: %s", i, e)
            chunk_summaries.append(f"[Chunk {i+1}: summarization failed]")

    # If multiple chunks, do a final merge pass
    if len(chunk_summaries) > 1:
        merged_text = "\n\n---\n\n".join(chunk_summaries)
        if len(merged_text) > CHUNK_SIZE:
            try:
                return _summarize_text(merged_text, model)
            except Exception:
                pass
        return merged_text

    return chunk_summaries[0]


def compact_messages(state: AgentState) -> AgentState:
    """Compact conversation: summarize old messages, keep recent ones.

    1. Take messages[:-KEEP_RECENT] and summarize with identity preservation
    2. Flush key facts to Mem0 + FTS5
    3. Replace with [summary] + messages[-KEEP_RECENT:]
    """
    messages = state.get("messages", [])
    if len(messages) <= MAX_MESSAGES:
        return {}

    user_id = state.get("user_id", "")
    session_id = state.get("session_id", "")

    # Split: old messages to summarize, recent to keep
    old_messages = messages[:-KEEP_RECENT]
    recent_messages = messages[-KEEP_RECENT:]

    conversation_text = _build_conversation_text(old_messages)
    if not conversation_text:
        return {}

    # Chunk-based summarization with identity preservation
    try:
        summary = _chunk_summarize(conversation_text)
    except Exception as e:
        log.error("Compaction summarization failed: %s", e)
        summary = f"[Conversation compacted: {len(old_messages)} messages removed]"

    # Flush facts to Mem0 + FTS5
    if user_id:
        try:
            fact_messages = [
                {"role": "user", "content": f"Conversation summary to remember:\n{summary}"},
            ]
            add_memories(fact_messages, user_id, session_id)
        except Exception as e:
            log.error("Compaction memory flush failed: %s", e)

    # Replace messages: summary + recent
    summary_msg = SystemMessage(
        content=f"[Previous conversation summary]\n{summary}"
    )

    log.info(
        "Compacted: %d messages → summary (%d chars) + %d recent",
        len(old_messages), len(summary), len(recent_messages),
    )

    return {"messages": [summary_msg] + list(recent_messages)}
