"""L4 Sleep-time Compute — nightly memory consolidation.

Runs as a cron job (3:00 UTC = 11:00 UTC+8).
Pipeline:
1. Deduplicate similar facts in FTS5 + Mem0
2. Extract entities from recent facts → Knowledge Graph
3. Build/update relations between entities
4. Generate insights from patterns
5. Clean up stale memories (>90 days, low relevance)
"""

import json
import logging
from datetime import UTC, datetime, timedelta

from kronos.cron.notify import TOPIC_GENERAL, send_bot_api
from kronos.llm import ModelTier, get_model
from kronos.memory import fts
from kronos.memory import knowledge_graph as kg

log = logging.getLogger("kronos.cron.sleep_compute")

ENTITY_EXTRACTION_PROMPT = """Extract entities and relationships from these memory facts.

Facts:
{facts}

For each entity found, output JSON:
{{
  "entities": [
    {{"name": "...", "type": "person|company|project|concept|tool|location", "properties": {{}}}}
  ],
  "relations": [
    {{"source": "...", "source_type": "...", "target": "...", "target_type": "...", "relation": "knows|works_at|uses|owns|related_to|part_of|created"}}
  ]
}}

Rules:
- Only extract clearly stated facts, don't infer
- Normalize names (e.g. "Роман", "John Doe" → "John Doe")
- Use English for entity types and relation types
- Skip vague or uncertain references
"""

INSIGHT_PROMPT = """Analyze these knowledge graph entities and their connections.

Entities and connections:
{graph_context}

Recent facts (last 7 days):
{recent_facts}

Generate 1-3 actionable insights. Examples:
- "User has been discussing Project Alpha and Project Beta frequently — both are in active development phase"
- "Multiple tools mentioned (LangGraph, Mem0, Playwright) suggest a focus on AI agent infrastructure"

Rules:
- Only insights supported by data
- Actionable (what could be done with this knowledge)
- Max 3 insights
- Russian language

Output as JSON array of strings.
"""


async def run_sleep_compute() -> None:
    """Run nightly memory consolidation."""
    log.info("Sleep-time compute starting...")

    # 1. Get recent facts from FTS5
    recent_facts = _get_recent_facts(days=7)
    if len(recent_facts) < 5:
        log.info("Only %d recent facts, skipping sleep compute", len(recent_facts))
        return

    # 2. Extract entities and build knowledge graph
    entities_added, relations_added = await _extract_and_build_graph(recent_facts)

    # 3. Generate insights
    insights = await _generate_insights(recent_facts)

    # 4. Ebbinghaus memory decay
    decay_stats = fts.decay_all_facts(half_life_days=14)

    # 5. Cleanup stale memories (archived + old)
    cleaned = _cleanup_stale_facts(days=90)

    # 6. Report
    stats = kg.get_stats()
    tier_dist = fts.get_tier_distribution()
    tier_str = " | ".join(f"{t}: {c}" for t, c in sorted(tier_dist.items()))

    summary = (
        f"🌙 Sleep Compute завершён\n"
        f"Entities: +{entities_added} (total: {stats['entities']})\n"
        f"Relations: +{relations_added} (total: {stats['relations']})\n"
        f"Decay: {decay_stats['decayed']} facts, {decay_stats['tier_changes']} tier changes\n"
        f"Tiers: {tier_str}\n"
        f"Cleaned: {cleaned} stale facts\n"
    )

    if insights:
        summary += "\n💡 Инсайты:\n" + "\n".join(f"• {i}" for i in insights)

    log.info(summary)
    send_bot_api(summary, topic_id=TOPIC_GENERAL)


def _get_recent_facts(days: int = 7) -> list[str]:
    """Get recent facts from FTS5 index."""
    conn = fts._get_conn()
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()

    rows = conn.execute(
        "SELECT content FROM memory_facts WHERE created_at > ? ORDER BY created_at DESC LIMIT 100",
        (cutoff,),
    ).fetchall()

    return [row["content"] for row in rows]


async def _extract_and_build_graph(facts: list[str]) -> tuple[int, int]:
    """Extract entities from facts and add to knowledge graph."""
    facts_text = "\n".join(f"- {f[:200]}" for f in facts[:50])

    prompt = ENTITY_EXTRACTION_PROMPT.format(facts=facts_text)

    model = get_model(ModelTier.LITE)
    from langchain_core.messages import HumanMessage
    response = model.invoke([HumanMessage(content=prompt)])
    reply = response.content if isinstance(response.content, str) else str(response.content)

    # Parse JSON from response
    import re
    match = re.search(r'\{[\s\S]*\}', reply)
    if not match:
        log.warning("No JSON in entity extraction response")
        return 0, 0

    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        log.warning("Invalid JSON in entity extraction response")
        return 0, 0

    entities_added = 0
    relations_added = 0

    for entity in data.get("entities", []):
        name = entity.get("name", "").strip()
        etype = entity.get("type", "concept").strip()
        props = entity.get("properties", {})
        if name:
            kg.add_entity(name, etype, props)
            entities_added += 1

    for rel in data.get("relations", []):
        src = rel.get("source", "").strip()
        src_type = rel.get("source_type", "concept").strip()
        tgt = rel.get("target", "").strip()
        tgt_type = rel.get("target_type", "concept").strip()
        rtype = rel.get("relation", "related_to").strip()
        if src and tgt:
            kg.add_relation(src, src_type, tgt, tgt_type, rtype)
            relations_added += 1

    log.info("KG updated: +%d entities, +%d relations", entities_added, relations_added)
    return entities_added, relations_added


async def _generate_insights(recent_facts: list[str]) -> list[str]:
    """Generate insights from knowledge graph + recent facts."""
    stats = kg.get_stats()
    if stats["entities"] < 3:
        return []

    # Get graph context
    conn = kg._get_conn()
    rows = conn.execute(
        "SELECT name, type FROM entities ORDER BY updated_at DESC LIMIT 20"
    ).fetchall()

    graph_parts = []
    for row in rows:
        connections = kg.get_connections(row["name"])
        if connections:
            conns = ", ".join(f"{c['relation']}→{c['entity']}" for c in connections[:3])
            graph_parts.append(f"{row['name']} ({row['type']}): {conns}")
        else:
            graph_parts.append(f"{row['name']} ({row['type']})")

    graph_context = "\n".join(graph_parts)
    facts_text = "\n".join(f"- {f[:150]}" for f in recent_facts[:20])

    prompt = INSIGHT_PROMPT.format(graph_context=graph_context, recent_facts=facts_text)

    model = get_model(ModelTier.LITE)
    from langchain_core.messages import HumanMessage

    try:
        response = model.invoke([HumanMessage(content=prompt)])
        reply = response.content if isinstance(response.content, str) else str(response.content)

        import re
        match = re.search(r'\[[\s\S]*\]', reply)
        if match:
            return json.loads(match.group())
    except Exception as e:
        log.error("Insight generation failed: %s", e)

    return []


def _cleanup_stale_facts(days: int = 90) -> int:
    """Remove facts older than N days from FTS5."""
    conn = fts._get_conn()
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()

    # Get IDs to delete
    rows = conn.execute(
        "SELECT id FROM memory_facts WHERE created_at < ?",
        (cutoff,),
    ).fetchall()

    if not rows:
        return 0

    ids = [row["id"] for row in rows]
    for fact_id in ids:
        conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (fact_id,))
        conn.execute("DELETE FROM memory_facts WHERE id = ?", (fact_id,))

    conn.commit()
    log.info("Cleaned %d stale facts (>%d days)", len(ids), days)
    return len(ids)
