"""Score node — evaluate and rank validated topics."""

import json
import logging

from langchain_core.messages import HumanMessage

from kronos.agents.topic_research.prompts import SCORE_PROMPT
from kronos.agents.topic_research.nodes.discover import _parse_json_array
from kronos.agents.topic_research.state import TopicResearchState
from kronos.llm import ModelTier, get_model

log = logging.getLogger("kronos.agents.topic_research.score")


async def score_topics(state: TopicResearchState) -> dict:
    """Score each topic on 5 weighted criteria."""
    validated = state.get("validated_topics", [])
    domain = state.get("domain", "")
    audience = state.get("target_audience", "developers and tech leaders")

    if not validated:
        return {"scored_topics": []}

    # Prepare topics JSON for LLM (truncated to fit context)
    topics_for_scoring = []
    for t in validated[:30]:
        topics_for_scoring.append({
            "title_en": t.get("title_en", t.get("title", "")),
            "title_ru": t.get("title_ru", ""),
            "primary_keyword": t.get("primary_keyword", ""),
            "unique_angle": t.get("unique_angle", ""),
            "content_brief": t.get("content_brief", ""),
            "validation": t.get("validation", {}),
        })

    topics_json = json.dumps(topics_for_scoring, ensure_ascii=False, indent=2)

    prompt = SCORE_PROMPT.format(
        domain=domain,
        target_audience=audience,
        topics_json=topics_json[:6000],
    )

    model = get_model(ModelTier.STANDARD)
    response = model.invoke([HumanMessage(content=prompt)])
    reply = response.content if isinstance(response.content, str) else str(response.content)

    scored = _parse_json_array(reply)

    # Merge scores back into validated topics
    scored_topics = []
    for i, topic in enumerate(validated[:30]):
        if i < len(scored) and isinstance(scored[i], dict):
            topic.update(scored[i])
        # Ensure total_score exists
        if "total_score" not in topic:
            topic["total_score"] = _calculate_score(topic)
        # Assign priority
        score = topic.get("total_score", 0)
        if score >= 75:
            topic["priority"] = "High"
        elif score >= 60:
            topic["priority"] = "Medium"
        else:
            topic["priority"] = "Low"
        scored_topics.append(topic)

    # Sort by score descending
    scored_topics.sort(key=lambda t: t.get("total_score", 0), reverse=True)

    high = sum(1 for t in scored_topics if t.get("priority") == "High")
    med = sum(1 for t in scored_topics if t.get("priority") == "Medium")
    log.info("Scored %d topics: %d High, %d Medium", len(scored_topics), high, med)

    return {"scored_topics": scored_topics}


def evaluate_quality(state: TopicResearchState) -> str:
    """Check if we have enough quality topics or need another iteration."""
    scored = state.get("scored_topics", [])
    iteration = state.get("iteration", 0)
    threshold = state.get("quality_threshold", 60)
    max_iterations = 2

    quality_topics = [t for t in scored if t.get("total_score", 0) >= threshold]

    if len(quality_topics) >= 10 or iteration >= max_iterations:
        return "format"
    else:
        log.info(
            "Quality check: only %d topics above %d (iter %d/%d), retrying",
            len(quality_topics), threshold, iteration, max_iterations,
        )
        return "discover"


def _calculate_score(topic: dict) -> int:
    """Fallback score calculation from validation data."""
    v = topic.get("validation", {})
    demand = v.get("search_demand", 50)
    comp = v.get("competition_level", "medium")
    comp_map = {"low": 80, "medium": 50, "high": 20, "unknown": 50}
    gap = comp_map.get(comp, 50)

    return int(demand * 0.25 + gap * 0.20 + 50 * 0.20 + 50 * 0.20 + 50 * 0.15)
