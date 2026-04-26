"""Daily Pulse — aggregates all data sources into a health digest."""

import logging
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage

from kronos.analytics.sources import zabbix, grafana, sentry
from kronos.analytics.sources import posthog, app_store, supabase_stats, web_analytics
from kronos.analytics.sources import revenuecat, litellm, langfuse_stats
from kronos.llm import ModelTier, get_model

log = logging.getLogger("kronos.analytics.pulse")


PULSE_PROMPT = """You are an internal analyst for the monitored product.

Here is the current data across all systems:

## Infrastructure (Zabbix)
{zabbix_data}

## Monitoring (Grafana)
{grafana_data}

## Errors (Sentry)
{sentry_data}

## Product Analytics (PostHog)
{posthog_data}

## App Store (iOS + Android)
{app_store_data}

## Database (Supabase)
{supabase_data}

## Web Analytics
{web_data}

## Revenue (RevenueCat)
{revenuecat_data}

## AI Costs (LiteLLM)
{litellm_data}

## LLM Quality (Langfuse)
{langfuse_data}

Generate a concise daily pulse:
1. Overall status: 🟢 All OK / 🟡 Issues detected / 🔴 Critical
2. 👥 Users & Product (DAU, signups, key features — 2-3 lines)
3. 💰 Revenue (MRR, subscribers — 1 line, if available)
4. 📱 App Store (ratings — 1 line)
5. 🖥 Infrastructure (servers, errors — 2-3 lines)
6. 🤖 AI (spend, requests — 1 line, if available)
7. 🌐 Web (traffic — 1 line)
8. ⚠️ Issues requiring attention (if any)
9. 💡 One actionable insight based on the data

If a data source returned an error, note it as "⚠️ Source unavailable" — don't skip the section.

Write in Russian. Format for Telegram — use emoji, keep it under 1800 chars.
Be specific with numbers, don't be vague."""


def _collect_all() -> dict[str, dict]:
    """Collect data from all sources. Each source handles its own errors."""
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
    }


def _format_source(name: str, data: dict) -> str:
    """Format a data source's output for the LLM prompt."""
    if "error" in data and len(data) == 1:
        return f"⚠️ {name}: unavailable ({data['error'][:100]})"

    lines = []
    for k, v in data.items():
        if k == "error":
            lines.append(f"  ⚠️ partial error: {v[:100]}")
        elif isinstance(v, list):
            if v:
                lines.append(f"  {k}:")
                for item in v[:5]:
                    lines.append(f"    - {item}" if isinstance(item, str) else f"    - {item}")
        else:
            lines.append(f"  {k}: {v}")
    return "\n".join(lines) if lines else f"  No data from {name}"


async def generate_daily_pulse() -> tuple[str, dict]:
    """Generate daily health pulse.

    Returns:
        (pulse_text, raw_metrics) — formatted pulse and raw data for storage.
    """
    metrics = _collect_all()

    prompt = PULSE_PROMPT.format(
        zabbix_data=_format_source("Zabbix", metrics["zabbix"]),
        grafana_data=_format_source("Grafana", metrics["grafana"]),
        sentry_data=_format_source("Sentry", metrics["sentry"]),
        posthog_data=_format_source("PostHog", metrics["posthog"]),
        app_store_data=_format_source("App Store", metrics["app_store"]),
        supabase_data=_format_source("Supabase", metrics["supabase"]),
        web_data=_format_source("Web Analytics", metrics["web"]),
        revenuecat_data=_format_source("RevenueCat", metrics["revenuecat"]),
        litellm_data=_format_source("LiteLLM", metrics["litellm"]),
        langfuse_data=_format_source("Langfuse", metrics["langfuse"]),
    )

    model = get_model(ModelTier.LITE)
    response = model.invoke([HumanMessage(content=prompt)])
    pulse = response.content if isinstance(response.content, str) else str(response.content)

    return pulse, metrics


async def answer_health_query(question: str) -> str:
    """Answer an on-demand health question using current data."""
    metrics = _collect_all()

    sections = "\n".join(
        f"{name}: {_format_source(name, data)}"
        for name, data in metrics.items()
    )

    prompt = (
        f"User question: \"{question}\"\n\n"
        f"Current system data:\n{sections}\n\n"
        f"Answer concisely with specific numbers. Russian. Telegram format."
    )

    model = get_model(ModelTier.LITE)
    response = model.invoke([HumanMessage(content=prompt)])
    return response.content if isinstance(response.content, str) else str(response.content)
