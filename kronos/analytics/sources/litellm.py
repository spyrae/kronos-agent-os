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
    yesterday = now - timedelta(days=1)

    try:
        # 1) Daily spend totals over the last 24h.
        # Endpoint returns [{"date": "YYYY-MM-DD", "spend": float}, ...].
        spend_data = _api_get("/global/spend/logs", {
            "start_date": yesterday.strftime("%Y-%m-%d"),
            "end_date": now.strftime("%Y-%m-%d"),
        })
        if not isinstance(spend_data, list):
            spend_data = spend_data.get("data", []) if isinstance(spend_data, dict) else []

        spend_24h = sum(float(s.get("spend", 0) or 0) for s in spend_data)

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
        top_models_str = (
            ", ".join(f"{m['model']}: ${float(m['total_spend']):.2f}" for m in top)
            if top else "N/A"
        )
        models_tracked = len(active_models)

        return {
            "spend_24h_usd": round(spend_24h, 4),
            "top_models": top_models_str,
            "models_tracked": models_tracked,
        }

    except Exception as e:
        log.error("LiteLLM collect failed: %s", e)
        return {"error": str(e)}
