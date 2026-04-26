"""Memory graph nodes — retrieval and storage.

Retrieval: injects relevant memories into the conversation before LLM call.
Storage: saves conversation facts in background after response is sent.
         Also indexes raw conversation turns in FTS5 for keyword search.

Memory architecture (per Phase 3 + Phase 7 of the swarm refactor):
  * Per-agent Mem0 collection (Qdrant) — personal reflections, agent-style
    context. Isolated per agent to prevent cross-agent contamination.
  * Per-agent FTS5 (``memory_fts.db``) — keyword search over extracted facts.
  * Per-agent knowledge graph — entity/relation memory.
  * Shared user facts (``swarm.db:shared_user_facts``) — facts extracted
    from USER messages, readable and writable by all agents so the swarm
    has one view of the user. Classification rule is a heuristic in v1:
    user-sourced → shared, agent-sourced → personal.
"""

import asyncio
import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from kronos.config import settings
from kronos.memory import fts
from kronos.memory import knowledge_graph as kg
from kronos.memory.store import add_memories, search_memories
from kronos.state import AgentState

log = logging.getLogger("kronos.memory")


def retrieve_memories(state: AgentState) -> AgentState:
    """Retrieve relevant memories and inject as system context.

    Runs before call_model. Searches memories using the last user message
    and adds them as a SystemMessage so the LLM has context.
    """
    user_id = state.get("user_id", "")
    if not user_id:
        return {}

    # Get last user message
    last_user_msg = ""
    for msg in reversed(state["messages"]):
        if isinstance(msg, HumanMessage):
            last_user_msg = msg.content if isinstance(msg.content, str) else str(msg.content)
            break

    if not last_user_msg:
        return {}

    memories = search_memories(last_user_msg, user_id=user_id, limit=5)

    # L3: Knowledge Graph context
    graph_context = ""
    try:
        graph_context = kg.get_graph_context(last_user_msg, limit=3)
    except Exception as e:
        log.debug("Knowledge graph lookup failed: %s", e)

    # L4: Shared cross-agent user facts (swarm.db). Keeps all 6 agents on
    # the same page about who the user is / what they're working on.
    shared_facts: list[str] = []
    try:
        from kronos.swarm_store import get_swarm
        shared_facts = get_swarm().search_shared_facts(
            user_id=user_id, query=last_user_msg, limit=5,
        )
    except Exception as e:
        log.debug("Shared user facts lookup failed: %s", e)

    if not memories and not graph_context and not shared_facts:
        return {}

    # Inject memories + graph as a system message
    parts = []
    if shared_facts:
        shared_text = "\n".join(f"- {f}" for f in shared_facts)
        parts.append(f"[Shared user facts]\n{shared_text}")
    if memories:
        memory_text = "\n".join(f"- {m}" for m in memories)
        parts.append(f"[My memories]\n{memory_text}")
    if graph_context:
        parts.append(f"[Knowledge graph]\n{graph_context}")

    memory_msg = SystemMessage(content="\n\n".join(parts))

    log.info(
        "Injected %d shared + %d personal memories for user %s",
        len(shared_facts), len(memories), user_id,
    )
    return {"messages": [memory_msg]}


def store_memories_background(state: AgentState) -> AgentState:
    """Store conversation facts in Mem0.

    Extracts the last user-assistant turn and stores it.
    Runs as a graph node after response generation.
    """
    user_id = state.get("user_id", "")
    session_id = state.get("session_id", "")
    if not user_id:
        return {}

    # Find last user message and last AI response
    last_user = ""
    last_assistant = ""

    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage) and not last_assistant:
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            if content and "заблокирован" not in content:
                last_assistant = content
        elif isinstance(msg, HumanMessage) and not last_user:
            last_user = msg.content if isinstance(msg.content, str) else str(msg.content)
        if last_user and last_assistant:
            break

    if not last_user or not last_assistant:
        return {}

    messages = [
        {"role": "user", "content": last_user},
        {"role": "assistant", "content": last_assistant},
    ]

    log.info("Storing memories for user %s: %s", user_id, last_user[:60])

    # 1. Mem0 fact extraction + FTS5 indexing of extracted facts (per-agent)
    extracted_facts = add_memories(messages, user_id, session_id)

    # 2. Shared user facts: mirror user-sourced extractions into the
    #    cross-agent swarm ledger so every agent sees the same facts.
    #    Heuristic classifier (v1): any fact extracted from a turn that
    #    contains a USER message is eligible for sharing. This is loose
    #    on purpose — Mem0 already filters to "durable facts". If noise
    #    turns out to be a problem we add an LLM classifier pass here.
    if extracted_facts:
        try:
            from kronos.swarm_store import get_swarm
            swarm = get_swarm()
            added = 0
            for fact in extracted_facts:
                if swarm.add_shared_fact(
                    user_id=user_id,
                    fact=fact,
                    source_agent=settings.agent_name,
                ):
                    added += 1
            if added:
                log.info("Mirrored %d facts to shared_user_facts", added)
        except Exception as e:
            log.debug("Shared facts mirror failed: %s", e)

    # 3. Index raw conversation turn in FTS5 (catches exact phrases,
    #    names, URLs that Mem0 fact extraction might miss). Per-agent.
    try:
        turn_text = f"User: {last_user}\nKronos: {last_assistant}"
        fts.index_fact(turn_text, user_id)
    except Exception as e:
        log.debug("FTS5 conversation indexing failed: %s", e)

    return {}
