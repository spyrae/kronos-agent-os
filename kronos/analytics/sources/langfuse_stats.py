"""Langfuse data source — LLM quality metrics, trace stats via REST API."""

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from kronos.config import settings

log = logging.getLogger("kronos.analytics.sources.langfuse_stats")

_TIMEOUT = 15


def _api_get(path: str, params: dict | None = None) -> dict | list:
    """GET request to Langfuse API."""
    base = settings.langfuse_host.rstrip("/")
    url = base + "/api/public" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    # Langfuse uses Basic auth with public_key:secret_key
    import base64
    credentials = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
    ).decode()

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Basic {credentials}",
            "Accept": "application/json",
            "User-Agent": "Kronos-II/1.0",
        },
    )
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    return json.loads(resp.read())


def collect() -> dict:
    """Collect LLM quality metrics for daily pulse."""
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return {"error": "Langfuse not configured"}

    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)

    try:
        # Get traces for last 24h
        traces = _api_get("/traces", {
            "page": "1",
            "limit": "1",  # just to get total count from meta
            "fromTimestamp": yesterday.isoformat(),
        })

        total_traces = traces.get("meta", {}).get("totalItems", 0)

        # Get observations (generations) for cost/latency stats
        observations = _api_get("/observations", {
            "page": "1",
            "limit": "50",
            "type": "GENERATION",
            "fromStartTime": yesterday.isoformat(),
        })

        obs_list = observations.get("data", [])
        total_observations = observations.get("meta", {}).get("totalItems", 0)

        # Calculate stats from sample
        total_cost = sum(o.get("calculatedTotalCost", 0) or 0 for o in obs_list)
        latencies = [
            o.get("latency", 0) or 0 for o in obs_list if o.get("latency")
        ]
        avg_latency_ms = round(sum(latencies) / len(latencies)) if latencies else None

        # Error rate
        errors = sum(1 for o in obs_list if o.get("level") == "ERROR")
        error_rate = round(errors / len(obs_list) * 100, 1) if obs_list else 0

        return {
            "traces_24h": total_traces,
            "generations_24h": total_observations,
            "sample_cost_usd": round(total_cost, 4),
            "avg_latency_ms": avg_latency_ms,
            "error_rate_pct": error_rate,
        }

    except Exception as e:
        log.error("Langfuse stats collect failed: %s", e)
        return {"error": str(e)}
