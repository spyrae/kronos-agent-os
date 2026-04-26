"""Format node — produce final report and optionally save to Notion."""

import logging

from langchain_core.messages import AIMessage, HumanMessage

from kronos.agents.topic_research.prompts import FORMAT_PROMPT
from kronos.agents.topic_research.state import TopicResearchState
from kronos.llm import ModelTier, get_model

log = logging.getLogger("kronos.agents.topic_research.format")


async def format_output(state: TopicResearchState) -> dict:
    """Format scored topics into a final report."""
    scored = state.get("scored_topics", [])
    domain = state.get("domain", "")

    if not scored:
        return {"messages": [AIMessage(content="Не удалось найти подходящие темы. Попробуй другие seed keywords.")]}

    high = [t for t in scored if t.get("priority") == "High"]
    medium = [t for t in scored if t.get("priority") == "Medium"]

    # Top topics for the report
    top_topics_text = ""
    for i, topic in enumerate(scored[:10], 1):
        title = topic.get("title_en", topic.get("title", "?"))
        title_ru = topic.get("title_ru", "")
        score = topic.get("total_score", 0)
        priority = topic.get("priority", "?")
        keyword = topic.get("primary_keyword", "")
        angle = topic.get("unique_angle", "")
        brief = topic.get("content_brief", "")

        top_topics_text += (
            f"\n{i}. **{title}** ({title_ru})\n"
            f"   Score: {score} | Priority: {priority} | Keyword: {keyword}\n"
            f"   Angle: {angle}\n"
            f"   Brief: {brief}\n"
        )

    prompt = FORMAT_PROMPT.format(
        domain=domain,
        total_count=len(scored),
        high_count=len(high),
        medium_count=len(medium),
        top_topics=top_topics_text,
    )

    model = get_model(ModelTier.STANDARD)
    response = model.invoke([HumanMessage(content=prompt)])
    report = response.content if isinstance(response.content, str) else str(response.content)

    log.info("Topic research report: %d chars, %d topics", len(report), len(scored))

    return {"messages": [AIMessage(content=report)]}
