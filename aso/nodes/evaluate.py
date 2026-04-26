"""EVALUATE node — assess optimization impact using LLM.

Compares baseline vs post-change metrics.
Generates verdict, learnings, and next recommendations.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..llm import ask, Model
from ..state import ASOState

log = logging.getLogger("aso.nodes.evaluate")

PROMPT_FILE = Path(__file__).parent.parent / "prompts" / "evaluator.md"


def _build_eval_prompt(state: ASOState) -> str:
    """Build prompt for the evaluator LLM."""
    baseline = state.get("baseline_metrics", {})
    post = state.get("post_metrics", {})
    changes = state.get("changes_applied", {})
    plan = state.get("optimization_plan", {})

    sections = []

    # What was changed
    sections.append("## Применённые изменения\n")
    applied = changes.get("applied", [])
    for ch in applied:
        sections.append(
            f"- {ch.get('locale')}.{ch.get('field')}: "
            f"\"{ch.get('old_value', '')}\" → \"{ch.get('new_value', '')}\""
        )
    sections.append(f"\nОжидалось: {plan.get('expected_impact', '?')}")
    sections.append("")

    # Baseline metrics
    sections.append("## Метрики ДО изменения\n")
    sections.append(f"- Timestamp: {baseline.get('timestamp', '?')}")
    kw_before = baseline.get("keyword_positions", {})
    if kw_before:
        sections.append("- Keywords:")
        for kw, pos in sorted(kw_before.items(), key=lambda x: x[1]):
            sections.append(f"  #{pos} \"{kw}\"")
    ratings_before = baseline.get("ratings", {})
    if ratings_before.get("avg_rating"):
        sections.append(
            f"- Rating: {ratings_before['avg_rating']} "
            f"({ratings_before.get('total_ratings', '?')} ratings)"
        )
    sections.append("")

    # Post metrics
    sections.append("## Метрики ПОСЛЕ изменения\n")
    sections.append(f"- Timestamp: {post.get('timestamp', '?')}")
    kw_after = post.get("keyword_positions", {})
    if kw_after:
        sections.append("- Keywords:")
        for kw, pos in sorted(kw_after.items(), key=lambda x: x[1]):
            delta = ""
            if kw in kw_before:
                diff = kw_before[kw] - pos  # positive = improved
                delta = f" ({'+' if diff > 0 else ''}{diff})"
            sections.append(f"  #{pos} \"{kw}\"{delta}")
    ratings_after = post.get("ratings", {})
    if ratings_after.get("avg_rating"):
        sections.append(
            f"- Rating: {ratings_after['avg_rating']} "
            f"({ratings_after.get('total_ratings', '?')} ratings)"
        )
    sections.append("")

    # Measurement period
    wait_start = state.get("wait_started", "")
    wait_end = state.get("wait_ended", "")
    if wait_start and wait_end:
        sections.append(f"Период измерения: {wait_start[:10]} — {wait_end[:10]}")
        sections.append("")

    sections.append(
        "Оцени результат. Верни ТОЛЬКО JSON-объект с полями: "
        "verdict, metrics, learnings, next_recommendations. "
        "Без markdown-обёртки."
    )

    return "\n".join(sections)


async def evaluate(state: ASOState) -> dict:
    """Evaluate the impact of applied changes."""
    log.info("=== EVALUATE: assessing optimization impact ===")

    baseline = state.get("baseline_metrics", {})
    post = state.get("post_metrics", {})

    if not baseline or not post:
        log.warning("Missing baseline or post metrics, generating basic evaluation")
        return {
            "phase": "evaluate",
            "evaluation": {
                "verdict": "insufficient_data",
                "learnings": ["Недостаточно данных для оценки — метрики до или после отсутствуют"],
                "next_recommendations": ["Повторить цикл мониторинга"],
            },
        }

    system_prompt = PROMPT_FILE.read_text() if PROMPT_FILE.exists() else ""
    user_prompt = _build_eval_prompt(state)

    try:
        response = await ask(
            user_prompt,
            system=system_prompt,
            model=Model.FAST,
            temperature=0.2,
        )

        evaluation = _parse_evaluation(response)

        if evaluation:
            log.info("Evaluation: %s", evaluation.get("verdict"))
        else:
            log.warning("Failed to parse evaluation, using fallback")
            evaluation = _fallback_evaluation(baseline, post)

        return {
            "phase": "evaluate",
            "evaluation": evaluation,
        }

    except Exception as e:
        log.error("Evaluation failed: %s", e)
        return {
            "phase": "evaluate",
            "evaluation": {
                "verdict": "error",
                "learnings": [f"Ошибка оценки: {e}"],
                "next_recommendations": ["Проверить LLM-провайдер и повторить"],
            },
            "error": f"evaluation failed: {e}",
        }


def _parse_evaluation(response: str) -> dict | None:
    """Parse evaluation JSON from LLM response."""
    text = response.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
    return None


def _fallback_evaluation(baseline: dict, post: dict) -> dict:
    """Generate a basic evaluation without LLM."""
    kw_before = baseline.get("keyword_positions", {})
    kw_after = post.get("keyword_positions", {})

    improvements = 0
    regressions = 0

    for kw in set(kw_before) | set(kw_after):
        before = kw_before.get(kw)
        after = kw_after.get(kw)
        if before and after:
            if after < before:
                improvements += 1
            elif after > before:
                regressions += 1
        elif not before and after:
            improvements += 1  # newly appeared
        elif before and not after:
            regressions += 1  # disappeared

    if improvements > regressions:
        verdict = "success"
    elif improvements == regressions:
        verdict = "neutral"
    else:
        verdict = "failure"

    return {
        "verdict": verdict,
        "metrics": {
            "keywords_improved": improvements,
            "keywords_regressed": regressions,
        },
        "learnings": [
            f"Keywords: {improvements} улучшились, {regressions} ухудшились",
        ],
        "next_recommendations": [
            "Провести следующий цикл мониторинга для уточнения",
        ],
    }
