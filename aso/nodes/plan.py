"""PLAN node — generate optimization plan using LLM.

Takes the selected opportunity + current metadata,
generates specific metadata changes with rationale.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..llm import Model, ask
from ..state import ASOState

log = logging.getLogger("aso.nodes.plan")

PROMPT_FILE = Path(__file__).parent.parent / "prompts" / "strategist.md"


def _build_plan_prompt(state: ASOState) -> str:
    """Build prompt for the strategist LLM."""
    opp = state.get("selected_opportunity", {})
    metadata = state.get("metadata_ios", {})
    competitors = state.get("competitor_data", [])
    rankings = state.get("keyword_rankings", {})
    feedback = state.get("human_feedback")

    sections = []

    # Opportunity
    sections.append("## Выбранная возможность\n")
    sections.append(f"- Тип: {opp.get('type')}")
    sections.append(f"- Приоритет: {opp.get('priority')}")
    sections.append(f"- Платформа: {opp.get('platform')}")
    sections.append(f"- Локаль: {opp.get('locale')}")
    sections.append(f"- Описание: {opp.get('description')}")
    sections.append(f"- Ожидаемый эффект: {opp.get('expected_impact')}")
    if opp.get("data"):
        sections.append(f"- Данные: {json.dumps(opp['data'], ensure_ascii=False)}")
    sections.append("")

    # Current metadata
    sections.append("## Текущие метаданные\n")
    for locale, data in metadata.items():
        if locale == opp.get("locale", "en-US") or locale.startswith(opp.get("locale", "en")[:2]):
            sections.append(f"### {locale}")
            sections.append(f"- Title: {data.get('title', '—')}")
            sections.append(f"- Subtitle: {data.get('subtitle', '—')}")
            sections.append(f"- Keywords: {data.get('keywords', '—')}")
            desc = data.get("description", "")
            sections.append(f"- Description: {desc[:500]}")
            sections.append(f"- Version state: {data.get('_version_state', '?')}")
            sections.append("")

    # Keyword data
    if rankings:
        sections.append("## Keyword позиции\n")
        for key, data in rankings.items():
            pos = data.get("position")
            pos_str = f"#{pos}" if pos else "не найдено"
            sections.append(f"- \"{data.get('keyword')}\" ({data.get('country')}): {pos_str}")
        sections.append("")

    # Competitors
    if competitors:
        sections.append("## Конкуренты\n")
        for comp in competitors[:5]:
            sections.append(
                f"- {comp.get('competitor_name', '?')}: "
                f"rating {comp.get('average_rating', '?')}, "
                f"{comp.get('rating_count', '?')} ratings"
            )
        sections.append("")

    # Human feedback (revision)
    if feedback:
        sections.append("## Обратная связь от пользователя (УЧТИ ПРИ СОСТАВЛЕНИИ ПЛАНА)\n")
        sections.append(feedback)
        sections.append("")

    sections.append(
        "Создай конкретный план оптимизации. "
        "Верни ТОЛЬКО JSON-объект с полями: changes, expected_impact, risk_assessment, "
        "rollback_plan, measurement_period_days. Без markdown-обёртки."
    )

    return "\n".join(sections)


async def plan(state: ASOState) -> dict:
    """Generate an optimization plan for the selected opportunity."""
    opp = state.get("selected_opportunity")
    if not opp:
        log.warning("PLAN: no selected opportunity, skipping")
        return {"phase": "plan", "optimization_plan": None}

    log.info("=== PLAN: generating optimization for [%s] ===", opp.get("type"))

    system_prompt = PROMPT_FILE.read_text() if PROMPT_FILE.exists() else ""
    user_prompt = _build_plan_prompt(state)

    try:
        response = await ask(
            user_prompt,
            system=system_prompt,
            model=Model.FAST,
            temperature=0.4,
        )

        plan_data = _parse_plan(response)

        if not plan_data:
            log.error("Failed to parse plan from LLM response")
            return {
                "phase": "plan",
                "optimization_plan": None,
                "error": "plan generation failed: unparseable response",
            }

        log.info("Plan generated: %d changes", len(plan_data.get("changes", [])))
        return {
            "phase": "plan",
            "optimization_plan": plan_data,
            "error": None,
        }

    except Exception as e:
        log.error("Plan generation failed: %s", e)
        return {
            "phase": "plan",
            "optimization_plan": None,
            "error": f"plan generation failed: {e}",
        }


def _parse_plan(response: str) -> dict | None:
    """Parse plan JSON from LLM response."""
    text = response.strip()

    # Strip code fences
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        else:
            return None

    if not isinstance(data, dict):
        return None

    # Validate required fields
    if "changes" not in data:
        return None

    return data
