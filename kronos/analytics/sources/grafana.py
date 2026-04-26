"""Grafana data source — alerts, Prometheus metrics via REST API."""

import json
import logging
import urllib.parse
import urllib.request

from kronos.config import settings

log = logging.getLogger("kronos.analytics.sources.grafana")

_TIMEOUT = 15


def _api_get(path: str, params: dict | None = None) -> dict | list:
    """GET request to Grafana API."""
    url = settings.grafana_url.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {settings.grafana_service_account_token}",
            "Accept": "application/json",
        },
    )
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    return json.loads(resp.read())


def _prom_query(query: str) -> float | None:
    """Execute a PromQL instant query and return the scalar value."""
    try:
        # Grafana Cloud uses /api/datasources/proxy/{id}/api/v1/query
        # or the unified alerting API. Try the simpler Grafana-native proxy.
        result = _api_get("/api/ds/query", {
            # This endpoint is complex; fall back to datasource proxy
        })
    except Exception:
        pass

    # Simpler: use Grafana's built-in Prometheus datasource proxy
    try:
        data = _api_get("/api/datasources/proxy/1/api/v1/query", {
            "query": query,
        })
        results = data.get("data", {}).get("result", [])
        if results and results[0].get("value"):
            return float(results[0]["value"][1])
    except Exception as e:
        log.debug("Prom query failed (%s): %s", query[:50], e)

    return None


def collect() -> dict:
    """Collect monitoring metrics for daily pulse."""
    if not settings.grafana_service_account_token:
        return {"error": "Grafana not configured"}

    try:
        # Firing alerts
        try:
            alerts = _api_get("/api/v1/provisioning/alert-rules")
            # Alternatively: unified alerting
            alert_instances = _api_get("/api/alertmanager/grafana/api/v2/alerts")
            firing = [a for a in (alert_instances or []) if a.get("status", {}).get("state") == "active"]
            firing_names = [
                a.get("labels", {}).get("alertname", "unknown")
                for a in firing[:5]
            ]
        except Exception as e:
            log.debug("Grafana alerts fetch failed: %s", e)
            firing = []
            firing_names = []

        # Try Prometheus queries for key metrics
        error_rate = _prom_query(
            'sum(rate(http_requests_total{status=~"5.."}[1h])) / '
            'sum(rate(http_requests_total[1h])) * 100'
        )
        latency_p95 = _prom_query(
            'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[1h])) by (le))'
        )

        return {
            "firing_alerts": len(firing),
            "alert_names": firing_names,
            "error_rate_pct": round(error_rate, 2) if error_rate is not None else None,
            "latency_p95_ms": round(latency_p95 * 1000, 1) if latency_p95 is not None else None,
        }

    except Exception as e:
        log.error("Grafana collect failed: %s", e)
        return {"error": str(e)}
