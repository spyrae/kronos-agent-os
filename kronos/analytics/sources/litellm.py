"""LiteLLM data source — AI model spend, tokens, latency via Admin API."""

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from kronos.config import settings

log = logging.getLogger("kronos.analytics.sources.litellm")

_TIMEOUT = 15


def _api_get(path: str, params: dict | None = None) -> dict | list:
    """GET request to LiteLLM Admin API."""
    base = settings.litellm_base_url.rstrip("/")
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {settings.litellm_admin_key}",
            "Accept": "application/json",
        },
    )
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    return json.loads(resp.read())


def collect() -> dict:
    """Collect AI cost metrics for daily pulse."""
    if not settings.litellm_base_url or not settings.litellm_admin_key:
        return {"error": "LiteLLM not configured"}

    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)

    try:
        # Spend logs for last 24h
        spend_data = _api_get("/spend/logs", {
            "start_date": yesterday.strftime("%Y-%m-%d"),
            "end_date": now.strftime("%Y-%m-%d"),
        })

        if not isinstance(spend_data, list):
            spend_data = spend_data.get("data", []) if isinstance(spend_data, dict) else []

        total_spend = sum(s.get("spend", 0) for s in spend_data)
        total_tokens = sum(s.get("total_tokens", 0) for s in spend_data)
        total_requests = len(spend_data)

        # Breakdown by model
        by_model: dict[str, float] = {}
        for s in spend_data:
            model = s.get("model", "unknown")
            by_model[model] = by_model.get(model, 0) + s.get("spend", 0)

        # Top 3 models by spend
        top_models = sorted(by_model.items(), key=lambda x: x[1], reverse=True)[:3]
        top_models_str = ", ".join(f"{m}: ${v:.3f}" for m, v in top_models) if top_models else "N/A"

        # Average latency
        latencies = [s.get("completion_time", 0) for s in spend_data if s.get("completion_time")]
        avg_latency = round(sum(latencies) / len(latencies), 2) if latencies else None

        return {
            "spend_24h_usd": round(total_spend, 4),
            "total_tokens_24h": total_tokens,
            "total_requests_24h": total_requests,
            "top_models": top_models_str,
            "avg_latency_s": avg_latency,
        }

    except Exception as e:
        log.error("LiteLLM collect failed: %s", e)
        return {"error": str(e)}
