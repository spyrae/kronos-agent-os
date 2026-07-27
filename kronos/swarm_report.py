"""Weekly post-mortem of the swarm (moat 11.4).

Every number here already existed in the ledger; what was missing was one place
that puts them next to each other. "Impulse answered 40 times" means little on
its own — next to "cost per reply $0.12" and "two 👎, zero 👍" it becomes a
decision about whether Impulse should be quieter.

Reads only. Sources, all shared: `reply_claims` (who answered, at which tier),
`swarm_costs` (spend per agent), `feedback` (reactions), `sla_watch`
(owned-topic deadlines and escalations), `handoffs`, `council_sessions`,
`challenges`, `swarm_metrics`.

Deliberately absent: cost per topic. Spend is recorded per agent and per day,
never attributed to a subject, so a "most expensive topics" table would be an
invention. Owned topics are reported by activity and missed SLAs instead —
numbers that exist.
"""

import time

from kronos.swarm_store import get_swarm

PERIOD_DAYS = {"day": 1, "week": 7, "month": 30}
DEFAULT_PERIOD = "week"


def _period_days(period: str) -> int:
    if period not in PERIOD_DAYS:
        raise ValueError(f"unknown period '{period}' (expected one of {', '.join(PERIOD_DAYS)})")
    return PERIOD_DAYS[period]


def build_report(period: str = DEFAULT_PERIOD, *, now: float | None = None) -> dict:
    """Collect the period's swarm activity into one structure."""
    days = _period_days(period)
    now = time.time() if now is None else now
    since_ts = now - days * 86400
    since_day = time.strftime("%Y-%m-%d", time.gmtime(since_ts))

    swarm = get_swarm()

    replies = swarm.replies_by_agent(since_ts=since_ts)
    costs = swarm.costs_by_agent(since_day=since_day)
    watches = swarm.sla_watches(since_ts=since_ts, limit=1000)
    handoffs = swarm.handoffs_since(since_ts=since_ts)
    councils = swarm.councils_since(since_ts=since_ts)
    challenges = swarm.challenges(since_ts=since_ts, limit=1000)

    agents = _agent_rows(replies, costs, swarm, days)

    return {
        "period": period,
        "days": days,
        "since": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(since_ts)),
        "generated_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now)),
        "agents": agents,
        "totals": _totals(agents),
        "tiers": _tier_totals(replies),
        "collaboration": {
            "handoffs": len(handoffs),
            "handoffs_done": sum(1 for h in handoffs if h["state"] == "done"),
            "handoffs_failed": sum(1 for h in handoffs if h["state"] == "failed"),
            "councils": len(councils),
            "reviews": len(challenges),
            "objections": sum(1 for c in challenges if c["verdict"] == "challenge"),
            "reviews_unanswered": sum(1 for c in challenges if c["state"] == "timeout"),
        },
        "ownership": _ownership(watches),
        "metrics": swarm.get_metrics(),
        "feedback": swarm.get_satisfaction_rate(days=days),
    }


def _agent_rows(replies: list[dict], costs: dict[str, dict], swarm, days: int) -> list[dict]:
    """One row per agent that either answered or spent something."""
    per_agent: dict[str, dict] = {}
    for row in replies:
        agent = per_agent.setdefault(
            row["agent_name"],
            {"agent": row["agent_name"], "replies": 0, "tier1": 0, "tier2": 0, "tier3": 0},
        )
        count = int(row["replies"])
        agent["replies"] += count
        tier_key = f"tier{int(row['tier'])}"
        if tier_key in agent:
            agent[tier_key] += count

    for name, spend in costs.items():
        agent = per_agent.setdefault(name, {"agent": name, "replies": 0, "tier1": 0, "tier2": 0, "tier3": 0})
        agent["cost_usd"] = round(spend["cost_usd"], 4)
        agent["requests"] = spend["requests"]

    for name, agent in per_agent.items():
        agent.setdefault("cost_usd", 0.0)
        agent.setdefault("requests", 0)
        # Cost per reply is the number that makes "answered a lot" comparable
        # between a cheap generalist and an expensive specialist.
        agent["cost_per_reply"] = round(agent["cost_usd"] / agent["replies"], 4) if agent["replies"] else None
        satisfaction = swarm.get_satisfaction_rate(agent_name=name, days=days)
        agent["feedback_positive"] = satisfaction["positive"]
        agent["feedback_negative"] = satisfaction["negative"]

    return sorted(per_agent.values(), key=lambda row: (-row["replies"], row["agent"]))


def _totals(agents: list[dict]) -> dict:
    replies = sum(row["replies"] for row in agents)
    cost = sum(row["cost_usd"] for row in agents)
    return {
        "agents": len(agents),
        "replies": replies,
        "cost_usd": round(cost, 4),
        "cost_per_reply": round(cost / replies, 4) if replies else None,
    }


def _tier_totals(replies: list[dict]) -> dict:
    """Tier mix: how much of the swarm's output was asked for vs volunteered."""
    tiers = {"tier1": 0, "tier2": 0, "tier3": 0}
    for row in replies:
        key = f"tier{int(row['tier'])}"
        if key in tiers:
            tiers[key] += int(row["replies"])
    total = sum(tiers.values())
    tiers["total"] = total
    tiers["explicit_share"] = round(tiers["tier1"] / total * 100, 1) if total else None
    return tiers


def _ownership(watches: list[dict]) -> dict:
    """Owned topics by activity, and where the owner went silent."""
    topics: dict[str, dict] = {}
    for watch in watches:
        row = topics.setdefault(
            watch["topic"],
            {"topic": watch["topic"], "owner": watch["owner_agent"], "requests": 0, "escalated": 0, "answered": 0},
        )
        row["requests"] += 1
        if watch["state"] == "escalated":
            row["escalated"] += 1
        elif watch["state"] == "answered":
            row["answered"] += 1

    return {
        "watched": len(watches),
        "escalated": sum(1 for w in watches if w["state"] == "escalated"),
        "pending": sum(1 for w in watches if w["state"] == "waiting"),
        "topics": sorted(topics.values(), key=lambda row: (-row["requests"], row["topic"])),
    }


def render_summary(report: dict) -> str:
    """Compact digest for chat.

    Telegram has no tables, so this is bullets rather than a squeezed version of
    the markdown table — the same numbers in a shape the medium can render.
    """
    totals = report["totals"]
    tiers = report["tiers"]
    ownership = report["ownership"]
    collab = report["collaboration"]
    feedback = report["feedback"]

    lines = [
        f"📊 <b>Отчёт роя — {report['period']}</b>",
        f"с {report['since']}",
        "",
        f"Ответов: {totals['replies']} · расход: ${totals['cost_usd']:.2f}"
        + (f" (${totals['cost_per_reply']:.3f}/ответ)" if totals["cost_per_reply"] is not None else ""),
    ]

    if tiers["total"]:
        lines.append(f"Тиры: T1 {tiers['tier1']} · T2 {tiers['tier2']} · T3 {tiers['tier3']}")

    for row in report["agents"]:
        per_reply = f" · ${row['cost_per_reply']:.3f}/отв" if row["cost_per_reply"] is not None else ""
        marks = ""
        if row["feedback_positive"]:
            marks += f" 👍{row['feedback_positive']}"
        if row["feedback_negative"]:
            marks += f" 👎{row['feedback_negative']}"
        lines.append(
            f"• {row['agent']}: {row['replies']} отв (T{row['tier1']}/{row['tier2']}/{row['tier3']}) "
            f"· ${row['cost_usd']:.2f}{per_reply}{marks}"
        )

    if ownership["watched"]:
        lines.append("")
        lines.append(f"Темы под владением: {ownership['watched']}, эскалаций: {ownership['escalated']}")
        for row in ownership["topics"][:5]:
            lines.append(f"• {row['topic']} ({row['owner']}): {row['requests']} обр, эскалаций {row['escalated']}")

    lines.append("")
    lines.append(
        f"Передачи: {collab['handoffs']} · консилиумы: {collab['councils']} · "
        f"ревью: {collab['reviews']} (возражений {collab['objections']})"
    )
    if feedback["total"]:
        lines.append(f"Удовлетворённость: {feedback['satisfaction_rate']:.0f}% из {feedback['total']} реакций")

    return "\n".join(lines)


def render_markdown(report: dict) -> str:
    """Human-readable digest — what goes to Telegram and to the terminal."""
    totals = report["totals"]
    tiers = report["tiers"]
    collab = report["collaboration"]
    ownership = report["ownership"]

    lines = [
        f"# Отчёт роя — {report['period']} ({report['since']} → {report['generated_at']})",
        "",
        f"Ответов: **{totals['replies']}** · агентов активно: {totals['agents']} · "
        f"расход: **${totals['cost_usd']:.2f}**"
        + (f" (${totals['cost_per_reply']:.3f} / ответ)" if totals["cost_per_reply"] is not None else ""),
    ]

    if tiers["total"]:
        lines += [
            "",
            f"По тирам: прямое обращение {tiers['tier1']} · по релевантности {tiers['tier2']} · "
            f"реакция на коллегу {tiers['tier3']}"
            + (f" · доля прямых обращений {tiers['explicit_share']:.0f}%" if tiers["explicit_share"] else ""),
        ]

    if report["agents"]:
        lines += [
            "",
            "## Кто отвечал",
            "",
            "| Агент | Ответов | T1/T2/T3 | Расход | $/ответ | 👍 | 👎 |",
            "|---|---:|---|---:|---:|---:|---:|",
        ]
        for row in report["agents"]:
            per_reply = f"${row['cost_per_reply']:.3f}" if row["cost_per_reply"] is not None else "—"
            lines.append(
                f"| {row['agent']} | {row['replies']} | "
                f"{row['tier1']}/{row['tier2']}/{row['tier3']} | "
                f"${row['cost_usd']:.2f} | {per_reply} | "
                f"{row['feedback_positive']} | {row['feedback_negative']} |"
            )
    else:
        lines += ["", "Ответов за период не было."]

    if ownership["watched"]:
        lines += [
            "",
            "## Владение темами",
            "",
            f"Под наблюдением: {ownership['watched']} · эскалаций: {ownership['escalated']} · "
            f"ещё в ожидании: {ownership['pending']}",
            "",
            "| Тема | Владелец | Обращений | Ответил | Эскалаций |",
            "|---|---|---:|---:|---:|",
        ]
        for row in ownership["topics"]:
            lines.append(
                f"| {row['topic']} | {row['owner']} | {row['requests']} | {row['answered']} | {row['escalated']} |"
            )

    lines += [
        "",
        "## Сотрудничество",
        "",
        f"- Передачи: {collab['handoffs']} (выполнено {collab['handoffs_done']}, "
        f"провалено {collab['handoffs_failed']})",
        f"- Консилиумы: {collab['councils']}",
        f"- Ревью перед отправкой: {collab['reviews']} "
        f"(возражений {collab['objections']}, без ответа {collab['reviews_unanswered']})",
    ]

    feedback = report["feedback"]
    if feedback["total"]:
        lines += [
            "",
            f"Реакции: 👍 {feedback['positive']} · 👎 {feedback['negative']} · "
            f"удовлетворённость {feedback['satisfaction_rate']:.0f}%",
        ]

    duplicates = report["metrics"].get("duplicate_replies_avoided", 0)
    respected = report["metrics"].get("addressing_respected", 0)
    if duplicates or respected:
        lines += [
            "",
            f"Координация: предотвращено дублей {duplicates} · уважено адресаций {respected}",
        ]

    return "\n".join(lines)
