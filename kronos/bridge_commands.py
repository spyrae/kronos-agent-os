"""Deterministic slash-command / query handlers for the bridge.

Handlers that map text to a reply without the live Telegram client: runtime
model identity, ``/persona``, ``/stats``, ``/aso``, and the ``/osint``
predicate. Extracted from ``bridge.py`` and re-exported from ``kronos.bridge``.
Handlers that need the live client (``/observer``, the ``/osint`` runner) stay
in ``bridge.py``.
"""

from kronos.config import settings
from kronos.security.cost_guardian import get_guardian
from kronos.swarm_store import get_swarm


def _handle_runtime_info_query(text: str) -> str | None:
    """Answer simple runtime/model identity questions deterministically."""
    normalized = " ".join(text.strip().lower().replace("ё", "е").split())
    if not normalized:
        return None

    is_model_question = normalized.startswith("/model") or (
        len(normalized) <= 180
        and any(
            phrase in normalized
            for phrase in (
                "что у тебя за модель",
                "какая у тебя модель",
                "что за модель",
                "на какой модели",
                "какой llm",
                "какой backend",
                "какой бэкенд",
                "какой провайдер",
            )
        )
    )
    if not is_model_question:
        return None

    orchestrator_chain = settings.kaos_orchestrator_provider_chain.strip() or settings.kaos_standard_provider_chain
    return (
        "Сейчас верхний оркестратор KAOS подключён через "
        f"`{orchestrator_chain}`. Для `codex-cli` используется Codex/ChatGPT OAuth "
        f"и модель `{settings.kaos_codex_model}`.\n\n"
        "Важно: это модель оркестратора. Специализированные подагенты пока могут "
        "использовать свои standard/lite цепочки: "
        f"`standard={settings.kaos_standard_provider_chain}`, "
        f"`lite={settings.kaos_lite_provider_chain}`."
    )


async def _handle_persona_command(text: str) -> str | None:
    """Handle /persona [list | approve <id> | reject <id>]. Returns reply or None."""
    if not text.startswith("/persona"):
        return None

    from kronos import evolution

    parts = text.split()
    action = parts[1].lower() if len(parts) > 1 else "list"
    agent = settings.agent_name

    if action == "list":
        pending = evolution.list_pending(agent)
        if not pending:
            return "🧬 Нет предложений эволюции персоны."
        lines = ["🧬 Предложения эволюции персоны:"]
        for proposal in pending:
            lines.append(f"#{proposal['id']} → {proposal['target']}: {proposal['rationale'][:80]}")
        lines.append("\n/persona approve <id> · /persona reject <id>")
        return "\n".join(lines)

    if action in ("approve", "reject") and len(parts) > 2 and parts[2].isdigit():
        pid = int(parts[2])
        decided = evolution.decide_proposal(pid, agent, approved=(action == "approve"))
        if decided is None:
            return f"Предложение #{pid} не найдено или уже обработано."
        if action == "reject":
            return f"❌ Отклонил предложение #{pid}."
        path = evolution.apply_proposal(decided)
        get_swarm().incr_metric("persona_proposals_approved")
        return f"✅ Применил предложение #{pid} к {decided['target'].upper()}\n{path}"

    return "Использование: /persona [list | approve <id> | reject <id>]"


async def _handle_stats_command(text: str) -> str | None:
    """Handle /stats [today|week]. Returns reply text, or None if not /stats."""
    if not text.startswith("/stats"):
        return None

    from kronos.security.cost_stats import cost_report, swarm_cost_by_agent

    parts = text.split()
    period = "week" if len(parts) > 1 and parts[1].lower().startswith("week") else "today"
    period_ru = "неделя" if period == "week" else "сегодня"

    report = cost_report(period)
    total = report["total"]
    status = get_guardian().get_status()

    lines = [f"📊 Расходы ({period_ru}) — {settings.agent_name}"]
    if total["requests"] == 0:
        lines.append("Запросов пока нет.")
    else:
        for tier, stats in sorted(report["by_tier"].items()):
            lines.append(f"• {tier}: {stats['requests']} зпр · ${stats['cost']:.4f}")
        lines.append(f"• Итого: {total['requests']} зпр · ${total['cost']:.4f}")

    daily = status["daily_cost"]
    limit = status["daily_limit"] or 0
    pct = (daily / limit * 100) if limit else 0
    lines.append(f"\nДневной бюджет: ${daily:.2f} / ${limit:.2f} ({pct:.0f}%)")

    swarm = swarm_cost_by_agent(period)
    if len(swarm) > 1:
        lines.append(f"\nПо агентам ({period_ru}):")
        for agent, cost in sorted(swarm.items(), key=lambda kv: -kv[1]):
            lines.append(f"• {agent}: ${cost:.4f}")

    return "\n".join(lines)


async def _handle_aso_command(text: str) -> str | None:
    """Handle /aso commands. Returns reply text or None if not an ASO command."""
    if not text.startswith("/aso"):
        return None

    parts = text.strip().split(maxsplit=2)
    cmd = parts[1] if len(parts) > 1 else "help"

    from kronos.agents.aso import (
        aso_approve,
        aso_reject,
        aso_resume,
        aso_run,
        aso_skip,
        aso_status,
    )

    if cmd == "run":
        dry_run = "--dry-run" in text
        return await aso_run(dry_run=dry_run)
    elif cmd == "approve":
        return await aso_approve()
    elif cmd == "reject":
        comment = parts[2] if len(parts) > 2 else ""
        return await aso_reject(comment)
    elif cmd == "skip":
        return await aso_skip()
    elif cmd == "resume":
        return await aso_resume()
    elif cmd == "status":
        return await aso_status()
    else:
        return (
            "ASO команды:\n"
            "/aso run [--dry-run] — запустить цикл\n"
            "/aso status — текущий статус\n"
            "/aso approve — одобрить план\n"
            "/aso reject <комментарий> — отклонить\n"
            "/aso skip — пропустить цикл\n"
            "/aso resume — продолжить после ожидания"
        )


def _is_osint_command(text: str) -> bool:
    return text.strip().casefold().startswith("/osint")
