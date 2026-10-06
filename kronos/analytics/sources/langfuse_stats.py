"""Langfuse data source — LLM quality metrics, trace stats via REST API."""

import json
import logging
import urllib.parse
import urllib.request
from collections import Counter
from datetime import UTC, datetime, timedelta

from kronos.config import settings

log = logging.getLogger("kronos.analytics.sources.langfuse_stats")

_TIMEOUT = 15
_EXPOSURE_PROBE_ROUTE = "/v1/models"
_EXPOSURE_PROBE_OBSERVATIONS_PER_HOUR = 2


def _api_get(path: str, params: dict | None = None) -> dict | list:
    """GET request to Langfuse API."""
    base = settings.langfuse_host.rstrip("/")
    url = base + "/api/public" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    # Langfuse uses Basic auth with public_key:secret_key
    import base64

    credentials = base64.b64encode(f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()).decode()

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Basic {credentials}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; KronosNexus/1.0)",
        },
    )
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    return json.loads(resp.read())


def collect() -> dict:
    """Collect LLM quality metrics for daily pulse."""
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return {"error": "Langfuse not configured"}

    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)

    try:
        # Get traces for last 24h
        traces = _api_get(
            "/traces",
            {
                "page": "1",
                "limit": "1",  # just to get total count from meta
                "fromTimestamp": yesterday.isoformat(),
            },
        )
        if not isinstance(traces, dict):
            return {"error": "Unexpected Langfuse traces response"}

        total_traces = traces.get("meta", {}).get("totalItems", 0)

        # Get observations (generations) for cost/latency stats
        observations = _api_get(
            "/observations",
            {
                "page": "1",
                "limit": "50",
                "type": "GENERATION",
                "fromStartTime": yesterday.isoformat(),
            },
        )
        if not isinstance(observations, dict):
            return {"error": "Unexpected Langfuse observations response"}

        obs_list = observations.get("data", [])
        total_observations = observations.get("meta", {}).get("totalItems", 0)

        # Calculate stats from sample
        total_cost = sum(o.get("calculatedTotalCost", 0) or 0 for o in obs_list)
        latencies = [o.get("latency", 0) or 0 for o in obs_list if o.get("latency")]
        avg_latency_ms = round(sum(latencies) / len(latencies)) if latencies else None

        # Error rate over real model calls only.
        #
        # Two kinds of rows reach Langfuse without ever invoking a model, and
        # counting them put the rate at 42.6% on a day with no product errors:
        #   - requests the LiteLLM gateway rejected for having no API key —
        #     scanners probing /azure/.env, /v1/models and friends;
        #   - gateway probes (/health, /model/info), which arrive with no model
        #     and the literal placeholder "default-message-value".
        # They are reported separately so the scanning stays visible instead of
        # masquerading as LLM quality.
        def _is_unauthenticated(o: dict) -> bool:
            return "no api key passed in" in str(o.get("statusMessage", "")).lower()

        def _is_probe(o: dict) -> bool:
            if o.get("model"):
                return False
            return "default-message-value" in str(o.get("input", ""))

        # Our own external exposure probe asks the gateway for /v1/models
        # without a key every hour, to prove it still answers 401. On
        # 2026-10-06 all 50 keyless requests of the day were this probe, and
        # the pulse reported them as something to investigate.
        #
        # Langfuse records no address or user agent, so the route is all that
        # identifies the probe — and the sender picks the route. Writing off
        # every keyless /v1/models request would let anyone silence this
        # signal by using that route. So only the probe's own footprint is
        # written off: it leaves two observations per hourly run, and anything
        # beyond that in the same hour, or without a timestamp to place it,
        # is still counted as unauthenticated.
        def _has_probe_shape(o: dict) -> bool:
            metadata = o.get("metadata")
            route = metadata.get("user_api_key_request_route") if isinstance(metadata, dict) else None
            return _is_unauthenticated(o) and route == _EXPOSURE_PROBE_ROUTE

        probe_by_hour: Counter[str] = Counter()
        for o in obs_list:
            hour = str(o.get("startTime") or "")[:13]
            if (
                _has_probe_shape(o)
                and len(hour) == 13
                and probe_by_hour[hour] < _EXPOSURE_PROBE_OBSERVATIONS_PER_HOUR
            ):
                probe_by_hour[hour] += 1

        scored = [o for o in obs_list if not _is_unauthenticated(o) and not _is_probe(o)]
        exposure_probe = sum(probe_by_hour.values())
        unauthenticated = sum(1 for o in obs_list if _is_unauthenticated(o)) - exposure_probe
        errors = sum(1 for o in scored if o.get("level") == "ERROR")
        error_rate = round(errors / len(scored) * 100, 1) if scored else 0

        return {
            "traces_24h": total_traces,
            "generations_24h": total_observations,
            "sample_cost_usd": round(total_cost, 4),
            "avg_latency_ms": avg_latency_ms,
            "error_rate_pct": error_rate,
            "error_sample_size": len(scored),
            "unauthenticated_requests": unauthenticated,
            "exposure_probe_requests": exposure_probe,
        }

    except Exception as e:
        log.error("Langfuse stats collect failed: %s", e)
        return {"error": str(e)}
