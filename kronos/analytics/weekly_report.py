"""Weekly Business Report — full company health digest.

Runs Monday 09:00 UTC via cron. Aggregates all sources for 7-day view,
compares with previous week (Mem0), generates executive summary.
"""

import logging
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage

from kronos.analytics.sources import (
    zabbix, grafana, sentry,
    posthog, app_store, supabase_stats, web_analytics,
    revenuecat, litellm, langfuse_stats, linear_stats,
)
from kronos.llm import ModelTier, get_model

log = logging.getLogger("kronos.analytics.weekly_report")

WEEKLY_PROMPT = """Ты — бизнес-аналитик мониторируемого продукта.
Составь еженедельный отчёт за прошедшую неделю.

## Данные

### REVENUE
{revenuecat_data}

### PRODUCT (PostHog)
{posthog_data}

### APP STORE
{app_store_data}

### DATABASE (Supabase)
{supabase_data}

### WEB TRAFFIC
{web_data}

### AI COSTS (LiteLLM)
{litellm_data}

### LLM QUALITY (Langfuse)
{langfuse_data}

### DEVELOPMENT (Linear)
{linear_data}

### INFRASTRUCTURE
Zabbix: {zabbix_data}
Grafana: {grafana_data}
Sentry: {sentry_data}

### TRENDS (WoW analysis)
{trends_data}

### HISTORY (прошлые недели)
{history_context}

## Формат отчёта

1. 📊 **EXECUTIVE SUMMARY** (3 bullet points — самое важное)
2. 🏆 **WINS** (что хорошего на этой неделе)
3. ⚠️ **CONCERNS** (что беспокоит, тренды вниз)
4. 📈 **KEY METRICS** (компактная таблица, WoW если есть данные)
5. 🎯 **ACTION ITEMS** (3-5 конкретных действий на эту неделю)
6. 💰 **AI COST OPTIMIZATION** (если есть возможности сэкономить)

Пиши на русском. Telegram-friendly: emoji для структуры, конкретные цифры.
Максимум 2500 символов. Не будь размытым — каждое предложение должно содержать число или факт."""


def _format_source(name: str, data: dict) -> str:
    """Format source data for prompt."""
    if "error" in data and len(data) == 1:
        return f"⚠️ {name}: unavailable ({data['error'][:100]})"

    lines = []
    for k, v in data.items():
        if k == "error":
            lines.append(f"  ⚠️ partial: {v[:80]}")
        elif isinstance(v, list):
            if v:
                lines.append(f"  {k}:")
                for item in v[:5]:
                    lines.append(f"    - {item}")
        else:
            lines.append(f"  {k}: {v}")
    return "\n".join(lines) if lines else f"  No data from {name}"


def _collect_all() -> dict[str, dict]:
    """Collect from all sources."""
    return {
        "zabbix": zabbix.collect(),
        "grafana": grafana.collect(),
        "sentry": sentry.collect(),
        "posthog": posthog.collect(),
        "app_store": app_store.collect(),
        "supabase": supabase_stats.collect(),
        "web": web_analytics.collect(),
        "revenuecat": revenuecat.collect(),
        "litellm": litellm.collect(),
        "langfuse": langfuse_stats.collect(),
        "linear": linear_stats.collect(),
    }


async def _get_history_context() -> str:
    """Retrieve previous weekly metrics from Mem0 for WoW comparison."""
    try:
        from kronos.memory.mem0_client import get_mem0
        mem0 = get_mem0()
        results = mem0.search(
            "weekly business report metrics MRR DAU revenue",
            user_id="analytics",
            limit=4,
        )
        if results and results.get("results"):
            entries = []
            for r in results["results"][:4]:
                text = r.get("memory", r.get("text", ""))
                if text:
                    entries.append(text[:300])
            return "\n".join(entries) if entries else "Нет исторических данных (первый отчёт)."
        return "Нет исторических данных (первый отчёт)."
    except Exception as e:
        log.debug("Mem0 history retrieval failed: %s", e)
        return "История недоступна."


async def _save_to_mem0(summary: str, metrics: dict) -> None:
    """Save key metrics to Mem0 for future WoW comparison."""
    try:
        from kronos.memory.mem0_client import get_mem0
        mem0 = get_mem0()

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Extract key numbers for compact storage
        parts = [f"Weekly report {today}:"]

        rc = metrics.get("revenuecat", {})
        if "error" not in rc:
            parts.append(f"MRR=${rc.get('mrr')}, subscribers={rc.get('active_subscribers')}")

        ph = metrics.get("posthog", {})
        if "error" not in ph:
            parts.append(f"DAU={ph.get('dau')}, signups={ph.get('new_signups_24h')}")

        ll = metrics.get("litellm", {})
        if "error" not in ll:
            parts.append(f"AI_spend=${ll.get('spend_24h_usd')}")

        li = metrics.get("linear", {})
        if "error" not in li:
            parts.append(f"completed={li.get('completed_this_week')}, bugs={li.get('bugs_open')}")

        mem0.add(
            " | ".join(parts),
            user_id="analytics",
            metadata={"type": "weekly_metrics", "date": today},
        )
        log.info("Weekly metrics saved to Mem0")
    except Exception as e:
        log.warning("Failed to save weekly metrics to Mem0: %s", e)


async def generate_weekly_report() -> tuple[str, dict]:
    """Generate full weekly business report.

    Returns:
        (report_text, raw_metrics) — formatted report and raw data.
    """
    metrics = _collect_all()
    history = await _get_history_context()

    # Trend analysis from metric_store
    from kronos.analytics.trends import analyze_trends, format_trends_summary
    trends = analyze_trends()
    trends_text = format_trends_summary(trends)

    prompt = WEEKLY_PROMPT.format(
        revenuecat_data=_format_source("RevenueCat", metrics["revenuecat"]),
        posthog_data=_format_source("PostHog", metrics["posthog"]),
        app_store_data=_format_source("App Store", metrics["app_store"]),
        supabase_data=_format_source("Supabase", metrics["supabase"]),
        web_data=_format_source("Web", metrics["web"]),
        litellm_data=_format_source("LiteLLM", metrics["litellm"]),
        langfuse_data=_format_source("Langfuse", metrics["langfuse"]),
        linear_data=_format_source("Linear", metrics["linear"]),
        zabbix_data=_format_source("Zabbix", metrics["zabbix"]),
        grafana_data=_format_source("Grafana", metrics["grafana"]),
        sentry_data=_format_source("Sentry", metrics["sentry"]),
        trends_data=trends_text,
        history_context=history,
    )

    model = get_model(ModelTier.STANDARD)
    response = model.invoke([HumanMessage(content=prompt)])
    report = response.content if isinstance(response.content, str) else str(response.content)

    # Save metrics to Mem0 for next week's comparison
    await _save_to_mem0(report, metrics)

    return report, metrics
