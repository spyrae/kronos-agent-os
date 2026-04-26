"""Expand node — enrich topics with PAA, competitor analysis, Reddit."""

import json
import logging

from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool

from kronos.agents.topic_research.prompts import EXPAND_PROMPT
from kronos.agents.topic_research.nodes.discover import _find_tool, _parse_json_array
from kronos.agents.topic_research.state import TopicResearchState
from kronos.llm import ModelTier, get_model

log = logging.getLogger("kronos.agents.topic_research.expand")


async def expand_topics(state: TopicResearchState, tools: list[BaseTool]) -> dict:
    """Expand topics with PAA questions, competitor audit, Reddit mining."""
    raw_topics = state.get("raw_topics", [])
    seeds = state.get("seed_keywords", [])

    brave = _find_tool(tools, "brave")

    paa_questions = []
    competitor_topics = []

    # 1. PAA mining — search with question-style queries
    for keyword in seeds[:3]:
        paa_queries = [
            f"{keyword} questions",
            f"how to {keyword}",
            f"why {keyword}",
        ]
        for q in paa_queries:
            try:
                if brave:
                    result = await brave.ainvoke({"query": q, "count": 5})
                    result_str = str(result)
                    # Extract question-like strings from results
                    for line in result_str.split("\n"):
                        line = line.strip()
                        if "?" in line and len(line) > 20 and len(line) < 200:
                            paa_questions.append(line)
            except Exception as e:
                log.debug("PAA search failed: %s", e)

    # 2. Competitor content audit
    competitor_blogs = ["lenny's newsletter", "first round review", "a16z blog"]
    for blog in competitor_blogs[:2]:
        for keyword in seeds[:2]:
            try:
                if brave:
                    result = await brave.ainvoke({
                        "query": f"site:{blog.replace(' ', '')} {keyword}",
                        "count": 3,
                    })
                    competitor_topics.append(f"[{blog}] {result}")
            except Exception as e:
                log.debug("Competitor search failed: %s", e)

    # 3. Reddit mining
    for keyword in seeds[:2]:
        try:
            if brave:
                result = await brave.ainvoke({
                    "query": f"site:reddit.com {keyword} question advice",
                    "count": 5,
                })
                result_str = str(result)
                for line in result_str.split("\n"):
                    if "?" in line and len(line) > 20:
                        paa_questions.append(line)
        except Exception as e:
            log.debug("Reddit search failed: %s", e)

    # Deduplicate PAA
    paa_questions = list(dict.fromkeys(paa_questions))[:30]

    # LLM expansion
    if paa_questions or competitor_topics:
        prompt = EXPAND_PROMPT.format(
            current_count=len(raw_topics),
            paa_questions="\n".join(paa_questions[:15]),
            competitor_topics="\n".join(str(ct)[:200] for ct in competitor_topics[:5]),
        )

        model = get_model(ModelTier.STANDARD)
        response = model.invoke([HumanMessage(content=prompt)])
        reply = response.content if isinstance(response.content, str) else str(response.content)

        new_topics = _parse_json_array(reply)
        raw_topics = raw_topics + new_topics

    log.info(
        "Expanded: %d topics, %d PAA questions, %d competitor refs",
        len(raw_topics), len(paa_questions), len(competitor_topics),
    )

    return {
        "raw_topics": raw_topics,
        "paa_questions": paa_questions,
        "competitor_topics": competitor_topics,
    }
