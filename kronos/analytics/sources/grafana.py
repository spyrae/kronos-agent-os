"""Grafana data source — alerts + JourneyBay infra metrics from VictoriaMetrics.

Pulls real metrics that exist in the configured Prometheus datasource:
- langfuse_requests_total, langfuse_errors_total, langfuse_latency_p95
- mcp_requests_total

Note: original implementation queried generic ``http_requests_total`` /
``http_request_duration_seconds_bucket``; those metrics are absent in the
JourneyBay stack (no Prom-instrumented backend) and always returned null.
The current queries target what is actually being scraped.
"""

import json
import logging
import os
import urllib.parse
import urllib.request
from urllib.error import HTTPError

from kronos.config import settings

log = logging.getLogger("kronos.analytics.sources.grafana")

_TIMEOUT = 15

# Set GRAFANA_PROM_DATASOURCE_UID env var to override; default is the
# JourneyBay VictoriaMetrics datasource UID. Datasource proxy supports
# both numeric id and uid.
_DEFAULT_PROM_UID = "P4169E866C3094E38"


def _api_get(path: str, params: dict | None = None) -> dict | list:
    url = settings.grafana_url.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {settings.grafana_service_account_token}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; KronosNexus/1.0)",
        },
    )
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    return json.loads(resp.read())


def _prom_query(query: str) -> float | None:
    """Run PromQL instant query against the default Prometheus datasource via Grafana proxy."""
    uid = os.environ.get("GRAFANA_PROM_DATASOURCE_UID") or _DEFAULT_PROM_UID
    try:
        data = _api_get(
            f"/api/datasources/proxy/uid/{uid}/api/v1/query",
            {"query": query},
        )
        results = data.get("data", {}).get("result", [])
        if results and results[0].get("value"):
            return float(results[0]["value"][1])
    except HTTPError as e:
        log.debug("Prom query HTTP %d: %s", e.code, query[:60])
    except Exception as e:
        log.debug("Prom query failed (%s): %s", query[:50], e)
    return None


def collect() -> dict:
    """Collect monitoring metrics for daily pulse."""
    if not settings.grafana_service_account_token:
        return {"error": "Grafana not configured"}

    try:
        # Firing alerts via unified alerting
        try:
            alert_instances = _api_get("/api/alertmanager/grafana/api/v2/alerts")
            firing = [a for a in (alert_instances or []) if a.get("status", {}).get("state") == "active"]
            firing_names = [a.get("labels", {}).get("alertname", "unknown") for a in firing[:5]]
        except Exception as e:
            log.debug("Grafana alerts fetch failed: %s", e)
            firing = []
            firing_names = []

        # Langfuse stack health (request rate, error rate, p95 latency).
        lf_rps = _prom_query("sum(rate(langfuse_requests_total[5m]))")
        lf_err_rate = _prom_query("sum(rate(langfuse_errors_total[5m]))")
        lf_latency_p95 = _prom_query("avg(langfuse_latency_p95)")

        # MCP server traffic.
        mcp_rps = _prom_query("sum(rate(mcp_requests_total[5m]))")

        return {
            "firing_alerts": len(firing),
            "alert_names": firing_names,
            "langfuse_rps": round(lf_rps, 4) if lf_rps is not None else None,
            "langfuse_err_rps": round(lf_err_rate, 4) if lf_err_rate is not None else None,
            "langfuse_latency_p95_ms": round(lf_latency_p95, 1) if lf_latency_p95 is not None else None,
            "mcp_rps": round(mcp_rps, 4) if mcp_rps is not None else None,
        }

    except Exception as e:
        log.error("Grafana collect failed: %s", e)
        return {"error": str(e)}
