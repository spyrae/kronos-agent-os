"""ANALYZE node — find optimization opportunities using LLM.

Takes monitor snapshot, sends to DeepSeek for analysis,
returns structured list of opportunities.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..llm import Model, ask
from ..state import ASOState, Opportunity

log = logging.getLogger("aso.nodes.analyze")

PROMPT_FILE = Path(__file__).parent.parent / "prompts" / "analyst.md"


def _build_data_section(state: ASOState) -> str:
    """Format collected data as context for the LLM."""
    sections = []

    # Metadata
    metadata = state.get("metadata_ios", {})
    if metadata:
        sections.append("## Текущие метаданные iOS\n")
        for locale, data in metadata.items():
            sections.append(f"### {locale}")
            sections.append(f"- Title: {data.get('title', '—')}")
            sections.append(f"- Subtitle: {data.get('subtitle', '—')}")
            sections.append(f"- Keywords: {data.get('keywords', '—')}")
            desc = data.get("description", "")
            if desc:
                sections.append(f"- Description (first 300 chars): {desc[:300]}")
            sections.append("")

    # Keyword rankings
    rankings = state.get("keyword_rankings", {})
    if rankings:
        sections.append("## Позиции по ключевым словам\n")
        for key, data in rankings.items():
            pos = data.get("position")
            pos_str = f"#{pos}" if pos else "не в top-50"
            priority = data.get("priority", "")
            sections.append(f"- [{priority}] \"{data.get('keyword')}\" ({data.get('country')}): {pos_str}")
        sections.append("")

    # Reviews summary
    reviews = state.get("reviews_summary", {})
    if reviews:
        sections.append("## Рейтинги и отзывы\n")
        sections.append(f"- Средний рейтинг: {reviews.get('avg_rating', '—')}")
        sections.append(f"- Всего оценок: {reviews.get('total_ratings', '—')}")
        sections.append(f"- Рейтинг текущей версии: {reviews.get('current_version_rating', '—')}")
        sections.append("")

    # Competitors
    competitors = state.get("competitor_data", [])
    if competitors:
        sections.append("## Конкуренты\n")
        for comp in competitors:
            sections.append(
                f"- {comp.get('competitor_name', comp.get('name', '?'))}: "
                f"rating {comp.get('average_rating', '?')} "
                f"({comp.get('rating_count', '?')} ratings)"
            )
        sections.append("")

    return "\n".join(sections)


async def analyze(state: ASOState) -> dict:
    """Analyze collected data and identify opportunities."""
    log.info("=== ANALYZE: searching for opportunities ===")

    # Load system prompt
    system_prompt = PROMPT_FILE.read_text() if PROMPT_FILE.exists() else ""

    # Build data context
    data_section = _build_data_section(state)

    user_prompt = (
        "Проанализируй текущее состояние ASO для приложения и найди возможности для оптимизации.\n\n"
        f"{data_section}\n"
        "Верни ТОЛЬКО JSON-массив opportunities. Без markdown-обёртки, без пояснений."
    )

    try:
        response = await ask(
            user_prompt,
            system=system_prompt,
            model=Model.FAST,
            temperature=0.3,
        )

        # Parse JSON from response
        opportunities = _parse_opportunities(response)
        log.info("Found %d opportunities", len(opportunities))

        return {
            "phase": "analyze",
            "opportunities": opportunities,
            "error": None,
        }

    except Exception as e:
        log.error("Analysis failed: %s", e)
        return {
            "phase": "analyze",
            "opportunities": [],
            "error": f"analysis failed: {e}",
        }


def _parse_opportunities(response: str) -> list[Opportunity]:
    """Parse LLM response into structured opportunities."""
    # Strip markdown code fences if present
    text = response.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (fences)
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON array in the response
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1:
            data = json.loads(text[start : end + 1])
        else:
            log.error("Failed to parse opportunities JSON: %s", text[:200])
            return []

    if not isinstance(data, list):
        data = [data]

    # Validate and normalize
    valid = []
    for item in data:
        if not isinstance(item, dict):
            continue
        opp: Opportunity = {
            "id": f"{item.get('type', 'unknown')}_{len(valid)}",
            "type": item.get("type", "unknown"),
            "priority": item.get("priority", "medium"),
            "platform": item.get("platform", "ios"),
            "locale": item.get("locale", "en-US"),
            "description": item.get("description", ""),
            "expected_impact": item.get("expected_impact", ""),
            "effort": item.get("effort", "medium"),
            "data": item.get("data", {}),
        }
        valid.append(opp)

    return valid
