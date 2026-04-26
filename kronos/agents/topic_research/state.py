"""Topic Research Agent state definition."""

from typing import TypedDict


class TopicResearchState(TypedDict):
    """State for the Topic Research sub-agent."""

    messages: list  # list[BaseMessage]
    domain: str
    seed_keywords: list[str]
    target_audience: str
    blog_context: str

    # Pipeline data
    raw_topics: list[dict]
    paa_questions: list[str]
    competitor_topics: list[dict]
    validated_topics: list[dict]
    scored_topics: list[dict]

    # Control flow
    iteration: int  # max 2
    quality_threshold: int  # default 60
