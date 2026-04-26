"""Topic Research Agent — blog topic discovery pipeline.

Pipeline: discover → expand → validate → score → [quality check] → format

Finds, validates, and scores blog topics using real search data
(Brave Search, Exa) and LLM analysis.

No LangGraph — plain async workflow.
"""

import logging

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool

from kronos.agents.topic_research.nodes.discover import discover_topics
from kronos.agents.topic_research.nodes.expand import expand_topics
from kronos.agents.topic_research.nodes.format import format_output
from kronos.agents.topic_research.nodes.score import evaluate_quality, score_topics
from kronos.agents.topic_research.nodes.validate import validate_topics
from kronos.agents.topic_research.state import TopicResearchState
from kronos.engine import AgentResult

log = logging.getLogger("kronos.agents.topic_research")

MAX_ITERATIONS = 2


def create_topic_research_agent(tools: list[BaseTool]):
    """Build the Topic Research sub-agent as an async callable.

    Returns an async function that takes messages and returns AgentResult.
    """

    async def run(messages: list[BaseMessage]) -> AgentResult:
        """Run the topic research pipeline."""
        state: TopicResearchState = {
            "messages": list(messages),
            "domain": "",
            "seed_keywords": [],
            "target_audience": "",
            "blog_context": "",
            "raw_topics": [],
            "paa_questions": [],
            "competitor_topics": [],
            "validated_topics": [],
            "scored_topics": [],
            "iteration": 0,
            "quality_threshold": 60,
        }

        for iteration in range(MAX_ITERATIONS):
            state["iteration"] = iteration

            # Step 1: discover topics
            update = await discover_topics(state, tools)
            state.update(update)

            # Step 2: expand topics
            update = await expand_topics(state, tools)
            state.update(update)

            # Step 3: validate topics
            update = await validate_topics(state, tools)
            state.update(update)

            # Step 4: score topics
            update = await score_topics(state)
            state.update(update)

            # Step 5: check quality
            decision = evaluate_quality(state)
            if decision == "format":
                break
            # else: "discover" — loop back

        # Step 6: format output
        update = await format_output(state)
        state.update(update)

        # Extract final content from messages
        content = ""
        for msg in reversed(state["messages"]):
            if isinstance(msg, AIMessage):
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                if content:
                    break

        return AgentResult(
            messages=state["messages"],
            content=content or "Не удалось сформировать результат.",
        )

    run.__name__ = "topic_research_agent"
    run.__qualname__ = "topic_research_agent"
    log.info("Topic Research agent created")
    return run
