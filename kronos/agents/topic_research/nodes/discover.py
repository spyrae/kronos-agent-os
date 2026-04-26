"""Discover node — find raw topic ideas from search results."""

import json
import logging

from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool

from kronos.agents.topic_research.prompts import DISCOVER_PROMPT
from kronos.agents.topic_research.state import TopicResearchState
from kronos.llm import ModelTier, get_model

log = logging.getLogger("kronos.agents.topic_research.discover")


def _find_tool(tools: list[BaseTool], name_prefix: str) -> BaseTool | None:
    for t in tools:
        if t.name.startswith(name_prefix):
            return t
    return None


async def discover_topics(state: TopicResearchState, tools: list[BaseTool]) -> dict:
    """Search multiple sources and extract raw topic ideas."""
    domain = state.get("domain", "")
    seeds = state.get("seed_keywords", [])
    audience = state.get("target_audience", "developers and tech leaders")
    blog_context = state.get("blog_context", "futurecraft.pro — AI, DevTools, Automation blog")

    brave = _find_tool(tools, "brave")
    exa = _find_tool(tools, "exa")

    # Collect search results from multiple queries
    all_results = []

    for keyword in seeds[:5]:
        queries = [
            f"{keyword} blog 2026",
            f"{keyword} how to guide",
            f"{keyword} for startups",
        ]
        for q in queries:
            try:
                if brave:
                    result = await brave.ainvoke({"query": q, "count": 5})
                    all_results.append(f"[Brave: {q}]\n{result}")
            except Exception as e:
                log.debug("Brave search failed for '%s': %s", q, e)

        # Exa semantic search
        if exa:
            try:
                result = await exa.ainvoke({
                    "query": f"{keyword} insights analysis",
                    "numResults": 5,
                    "type": "auto",
                })
                all_results.append(f"[Exa: {keyword}]\n{result}")
            except Exception as e:
                log.debug("Exa search failed for '%s': %s", keyword, e)

    if not all_results:
        log.warning("No search results collected for discover")
        return {"raw_topics": []}

    search_data = "\n\n---\n\n".join(all_results[:20])

    prompt = DISCOVER_PROMPT.format(
        domain=domain,
        seed_keywords=", ".join(seeds),
        target_audience=audience,
        blog_context=blog_context,
    )

    model = get_model(ModelTier.STANDARD)
    response = model.invoke([
        HumanMessage(content=f"{prompt}\n\nРезультаты поиска:\n{search_data}"),
    ])
    reply = response.content if isinstance(response.content, str) else str(response.content)

    topics = _parse_json_array(reply)
    log.info("Discovered %d raw topics for domain '%s'", len(topics), domain)

    return {"raw_topics": topics, "iteration": state.get("iteration", 0) + 1}


def _parse_json_array(text: str) -> list[dict]:
    """Extract JSON array from LLM response (may contain markdown fences)."""
    import re
    # Try to find JSON array in the response
    match = re.search(r'\[[\s\S]*\]', text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return []
