"""LiteLLM data source — AI model spend via Admin API.

Uses two complementary endpoints:
- ``/global/spend/logs`` — daily spend totals over the time window
- ``/global/spend/models`` — per-model breakdown (all-time totals)

Earlier code mistakenly tried to derive ``model``, ``total_tokens`` and
``completion_time`` from ``/global/spend/logs``, but that endpoint only
returns ``[{date, spend}]`` — hence ``top_models: unknown``, ``tokens=0``
and ``latency=None`` in the daily pulse.
"""

import json
import logging
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

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
            "User-Agent": "Mozilla/5.0 (compatible; KronosNexus/1.0)",
        },
    )
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    return json.loads(resp.read())


def collect() -> dict:
    """Collect AI cost metrics for daily pulse."""
    if not settings.litellm_base_url or not settings.litellm_admin_key:
        return {"error": "LiteLLM not configured"}

    now = datetime.now(UTC)
    yesterday_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    today_date = now.strftime("%Y-%m-%d")
    seven_days_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")

    try:
        # /global/spend/logs returns [{"date": "YYYY-MM-DD", "spend": float}, ...]
        # IMPORTANT: LiteLLM v1.83 ignores start_date/end_date params and
        # always returns the last 30 days. So we filter locally.
        spend_data = _api_get(
            "/global/spend/logs",
            {
                "start_date": yesterday_date,
                "end_date": today_date,
            },
        )
        if not isinstance(spend_data, list):
            spend_data = spend_data.get("data", []) if isinstance(spend_data, dict) else []

        # 24h spend = today + yesterday entries.
        spend_24h = sum(
            float(s.get("spend", 0) or 0) for s in spend_data if str(s.get("date", "")) in (today_date, yesterday_date)
        )
        # 7d spend = entries with date >= 7-day cutoff.
        spend_7d = sum(float(s.get("spend", 0) or 0) for s in spend_data if str(s.get("date", "")) >= seven_days_ago)
        # 30d total (whatever LiteLLM returned).
        spend_30d = sum(float(s.get("spend", 0) or 0) for s in spend_data)

        # 2) Top models by all-time spend. The endpoint returns
        # [{"model": str, "total_spend": float}, ...] sorted desc.
        try:
            models = _api_get("/global/spend/models")
            if isinstance(models, dict):
                models = models.get("data", [])
        except Exception as e:
            log.debug("LiteLLM /global/spend/models failed: %s", e)
            models = []

        # Keep only models with non-zero spend, top 3 by total_spend.
        active_models = [m for m in (models or []) if (m.get("total_spend") or 0) > 0]
        active_models.sort(key=lambda m: m.get("total_spend") or 0, reverse=True)
        top = active_models[:3]
        top_models_str = ", ".join(f"{m['model']}: ${float(m['total_spend']):.2f}" for m in top) if top else "N/A"
        models_tracked = len(active_models)

        return {
            "spend_24h_usd": round(spend_24h, 4),
            "spend_7d_usd": round(spend_7d, 4),
            "spend_30d_usd": round(spend_30d, 4),
            "top_models": top_models_str,
            "models_tracked": models_tracked,
        }

    except Exception as e:
        log.error("LiteLLM collect failed: %s", e)
        return {"error": str(e)}
