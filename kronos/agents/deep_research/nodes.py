"""Nodes for the Deep Research agent graph.

Pipeline: classify → plan_queries → execute_searches → evaluate → synthesize

Search execution uses a ReAct agent (LLM decides how to call tools)
instead of hardcoded tool invocations. This lets the LLM use each tool's
correct parameter schema automatically.
"""

import json
import logging

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool

from kronos.agents.deep_research.state import DeepResearchState, SearchResult
from kronos.engine import create_agent
from kronos.llm import ModelTier, get_model

log = logging.getLogger("kronos.agents.deep_research")

# Tools and search agent (set during graph build)
_tools: list[BaseTool] = []
_search_agent = None

MAX_ITERATIONS = 2
MIN_QUALITY_SCORE = 60


def set_tools(tools: list[BaseTool], on_tool_event=None) -> None:
    """Register search tools and build a ReAct search agent."""
    global _tools, _search_agent
    _tools = [
        t for t in tools
        if any(kw in t.name.lower() for kw in (
            "brave", "exa", "fetch", "content", "extract", "reddit", "search", "transcript",
        ))
    ]
    if _tools:
        _search_agent = create_agent(
            model=get_model(ModelTier.LITE),
            tools=_tools,
            system_prompt="You are a search assistant. Execute the given search queries using the available tools. If a tool returns an error, skip it and try an alternative. Return all results.",
            name="search_executor",
            on_tool_event=on_tool_event,
        )
    log.info("Research tools registered: %d tools", len(_tools))


def classify_mode(state: DeepResearchState) -> DeepResearchState:
    """Classify research mode from user query."""
    # Find the actual user message (skip system/handoff messages)
    query = ""
    for msg in reversed(state["messages"]):
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if isinstance(msg, HumanMessage) and "transferred" not in content.lower():
            query = content
            break
    if not query:
        # Fallback: use last message regardless
        last_msg = state["messages"][-1]
        query = last_msg.content if isinstance(last_msg.content, str) else str(last_msg.content)

    model = get_model(ModelTier.LITE)
    prompt = f"""Classify this research request into exactly one mode.
Respond with ONLY the mode name, nothing else.

Modes:
- topic: deep dive into a subject ("расскажи про", "что такое", "как работает")
- validation: validate an idea ("проверь идею", "есть ли конкуренты", "стоит ли делать")
- market: market research ("анализ рынка", "pain points", "что людям нужно")
- competitive: analyze a competitor ("разбери [продукт]", "конкурентный анализ")
- trends: trend analysis ("тренды в", "что растёт", "trend analysis")

Request: {query}

Mode:"""

    response = model.invoke([HumanMessage(content=prompt)])
    mode_text = response.content.strip().lower()

    # Normalize
    mode_map = {
        "topic": "topic", "validation": "validation", "market": "market",
        "competitive": "competitive", "trends": "trends",
    }
    mode = mode_map.get(mode_text, "topic")

    log.info("Research mode: %s, topic: %s", mode, query[:80])
    return {"topic": query, "mode": mode, "iteration": 0, "search_results": [], "search_queries": []}


def plan_queries(state: DeepResearchState) -> DeepResearchState:
    """Plan search queries based on mode and topic."""
    model = get_model(ModelTier.LITE)
    available = [t.name for t in _tools]

    prompt = f"""Plan search queries for a {state['mode']} research on: "{state['topic']}"

Available search tools: {available}

Generate 5-7 search queries. For each, specify which tool to use.
Respond in JSON format:
[
  {{"query": "search query text", "tool": "brave"}},
  {{"query": "another query", "tool": "exa"}},
  ...
]

Rules:
- Use "brave" for broad web search and site:-specific searches
- Use "exa" for deep content search (academic, technical)
- Use "reddit" for community discussions and opinions
- Use "fetch" to extract full content from a specific URL
- Vary formulations — don't repeat the same query
- Use Russian and English queries as appropriate
- For validation: include site:github.com, site:producthunt.com searches

Return ONLY the JSON array, no other text."""

    response = model.invoke([HumanMessage(content=prompt)])
    content = response.content.strip()

    # Parse JSON from response
    try:
        # Handle markdown code blocks
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        queries = json.loads(content)
    except (json.JSONDecodeError, IndexError):
        log.warning("Failed to parse search plan, using defaults")
        queries = [
            {"query": state["topic"], "tool": "brave"},
            {"query": f"{state['topic']} обзор анализ", "tool": "brave"},
            {"query": state["topic"], "tool": "exa"} if "exa" in available else {"query": state["topic"], "tool": "brave"},
        ]

    log.info("Planned %d queries for '%s'", len(queries), state["topic"][:50])
    return {"search_queries": queries}


async def execute_searches(state: DeepResearchState) -> DeepResearchState:
    """Execute searches via ReAct agent (LLM picks correct tool params)."""
    if not _search_agent:
        log.warning("No search agent available, skipping searches")
        return {"iteration": state.get("iteration", 0) + 1}

    results: list[SearchResult] = list(state.get("search_results", []))
    queries = state.get("search_queries", [])

    # Build a single prompt for the search agent with all queries
    queries_text = "\n".join(
        f"- {q.get('query', '')} (use {q.get('tool', 'any available')} tool)"
        for q in queries if q.get("query")
    )

    search_prompt = f"""Execute these search queries and return ALL results.
For each query, use the most appropriate tool. Return the raw results.

Queries:
{queries_text}

Execute each query one by one. Return all results you find."""

    try:
        agent_result = await _search_agent([HumanMessage(content=search_prompt)])

        # Extract content from search agent's messages
        for msg in agent_result.messages:
            if isinstance(msg, AIMessage) and msg.content:
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                if len(content) > 100:
                    results.append(SearchResult(
                        query=state["topic"],
                        source="search_agent",
                        content=content[:5000],
                        url="",
                    ))

        log.info("Search agent returned %d results, total chars: %d",
                 len(results), sum(len(r["content"]) for r in results))

    except Exception as e:
        log.error("Search agent failed: %s", e)

    return {"search_results": results, "iteration": state.get("iteration", 0) + 1}


def evaluate_quality(state: DeepResearchState) -> DeepResearchState:
    """Evaluate if we have enough data or need more searches."""
    results = state.get("search_results", [])
    iteration = state.get("iteration", 0)

    # Simple heuristic: quality based on amount of data
    total_chars = sum(len(r["content"]) for r in results)
    unique_sources = len(set(r["source"] for r in results))

    if total_chars > 10000 and unique_sources >= 2:
        score = 80
    elif total_chars > 5000:
        score = 60
    elif total_chars > 2000:
        score = 40
    else:
        score = 20

    log.info(
        "Quality evaluation: score=%d, results=%d, chars=%d, sources=%d, iteration=%d",
        score, len(results), total_chars, unique_sources, iteration,
    )
    return {"quality_score": score}


def should_search_more(state: DeepResearchState) -> str:
    """Decide: search more or synthesize."""
    score = state.get("quality_score", 0)
    iteration = state.get("iteration", 0)

    if score >= MIN_QUALITY_SCORE or iteration >= MAX_ITERATIONS:
        return "synthesize"
    return "plan_more_queries"


def plan_more_queries(state: DeepResearchState) -> DeepResearchState:
    """Plan additional queries based on gaps in current results."""
    model = get_model(ModelTier.LITE)
    existing = [r["query"] for r in state.get("search_results", [])]

    prompt = f"""I'm researching: "{state['topic']}" (mode: {state['mode']})

Already searched:
{chr(10).join(f'- {q}' for q in existing)}

The current data is insufficient. Plan 3 additional search queries that:
- Cover different angles than what we already have
- Are more specific or targeted
- Use different tools if possible

Available tools: {[t.name for t in _tools]}

Respond in JSON format: [{{"query": "...", "tool": "..."}}]
Return ONLY the JSON array."""

    response = model.invoke([HumanMessage(content=prompt)])
    content = response.content.strip()

    try:
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        queries = json.loads(content)
    except (json.JSONDecodeError, IndexError):
        queries = [{"query": f"{state['topic']} detailed analysis", "tool": "brave"}]

    return {"search_queries": queries}


def synthesize_report(state: DeepResearchState) -> DeepResearchState:
    """Synthesize all search results into a structured report."""
    model = get_model(ModelTier.STANDARD)
    results = state.get("search_results", [])

    # Build context from search results
    context_parts = []
    for i, r in enumerate(results, 1):
        context_parts.append(f"[Source {i}: {r['source']}] Query: {r['query']}\n{r['content'][:2000]}")

    context = "\n\n---\n\n".join(context_parts)

    # Load report format from skill definition
    mode = state.get("mode", "topic")
    from kronos.workspace import ws
    skill_path = str(ws.skill_path("deep-research"))

    try:
        with open(skill_path, encoding="utf-8") as f:
            skill_content = f.read()
    except FileNotFoundError:
        skill_content = ""

    prompt = f"""Ты — Deep Research Agent (INTJ). Составь структурированный отчёт.

Тема: {state['topic']}
Режим: {mode}

{f"Следуй формату отчёта из skill definition для режима '{mode}':" if skill_content else ""}
{skill_content[:3000] if skill_content else ""}

Собранные данные ({len(results)} источников):

{context[:12000]}

Правила:
- Только факты с источниками. Не выдумывай.
- Если данных нет — пиши "данных нет"
- Начинай с TL;DR
- Русский язык, термины на EN
- Actionable recommendations в конце"""

    response = model.invoke([HumanMessage(content=prompt)])
    report = response.content if isinstance(response.content, str) else str(response.content)

    log.info("Report synthesized: %d chars from %d sources", len(report), len(results))
    return {
        "report": report,
        "messages": [AIMessage(content=report)],
    }
