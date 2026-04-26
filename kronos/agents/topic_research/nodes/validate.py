"""Validate node — check each topic against real SERP data."""

import json
import logging

from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool

from kronos.agents.topic_research.prompts import VALIDATE_PROMPT
from kronos.agents.topic_research.nodes.discover import _find_tool, _parse_json_array
from kronos.agents.topic_research.state import TopicResearchState
from kronos.llm import ModelTier, get_model

log = logging.getLogger("kronos.agents.topic_research.validate")


async def validate_topics(state: TopicResearchState, tools: list[BaseTool]) -> dict:
    """Validate each topic against real SERP data."""
    raw_topics = state.get("raw_topics", [])
    brave = _find_tool(tools, "brave")
    model = get_model(ModelTier.STANDARD)

    validated = []

    for topic in raw_topics[:30]:  # cap to avoid too many API calls
        title = topic.get("title_en", topic.get("title", ""))
        keyword = topic.get("primary_keyword", title)

        if not keyword:
            continue

        # SERP check
        serp_results = ""
        if brave:
            try:
                result = await brave.ainvoke({"query": keyword, "count": 5})
                serp_results = str(result)[:2000]
            except Exception as e:
                log.debug("SERP check failed for '%s': %s", keyword, e)

        if not serp_results:
            # Skip if we can't validate
            topic["validation"] = {"search_demand": 50, "competition_level": "unknown"}
            validated.append(topic)
            continue

        # LLM validation
        prompt = VALIDATE_PROMPT.format(
            topic_title=title,
            primary_keyword=keyword,
            serp_results=serp_results,
        )

        try:
            response = model.invoke([HumanMessage(content=prompt)])
            reply = response.content if isinstance(response.content, str) else str(response.content)

            # Parse validation result
            validation = _parse_json_object(reply)
            if validation:
                topic["validation"] = validation
            else:
                topic["validation"] = {"search_demand": 50, "competition_level": "unknown"}
        except Exception as e:
            log.debug("Validation LLM failed for '%s': %s", title, e)
            topic["validation"] = {"search_demand": 50, "competition_level": "unknown"}

        validated.append(topic)

    log.info("Validated %d/%d topics", len(validated), len(raw_topics))
    return {"validated_topics": validated}


def _parse_json_object(text: str) -> dict | None:
    """Extract JSON object from LLM response."""
    import re
    match = re.search(r'\{[\s\S]*\}', text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None
