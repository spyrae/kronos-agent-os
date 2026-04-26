"""RevenueCat data source — MRR, subscribers, churn via REST API v2."""

import json
import logging
import urllib.parse
import urllib.request

from kronos.config import settings

log = logging.getLogger("kronos.analytics.sources.revenuecat")

_TIMEOUT = 15
_BASE_URL = "https://api.revenuecat.com/v2"


def _api_get(path: str, params: dict | None = None) -> dict:
    """GET request to RevenueCat V2 API."""
    url = _BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {settings.revenuecat_api_key}",
            "Accept": "application/json",
        },
    )
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    return json.loads(resp.read())


def collect() -> dict:
    """Collect revenue metrics for daily/weekly pulse."""
    if not settings.revenuecat_api_key or not settings.revenuecat_project_id:
        return {"error": "RevenueCat not configured"}

    project_id = settings.revenuecat_project_id

    try:
        # Overview metrics — returns {"metrics": [{"id": "mrr", "value": 123}, ...]}
        overview = _api_get(f"/projects/{project_id}/metrics/overview")
        metrics_list = overview.get("metrics", [])

        # Convert list to dict keyed by metric id
        metrics = {m["id"]: m.get("value") for m in metrics_list if "id" in m}

        return {
            "mrr": metrics.get("mrr"),
            "active_subscriptions": metrics.get("active_subscriptions"),
            "active_trials": metrics.get("active_trials"),
            "revenue": metrics.get("revenue"),
            "new_customers": metrics.get("new_customers"),
        }

    except urllib.error.HTTPError as e:
        # RevenueCat V2 may have different endpoint structure
        log.error("RevenueCat API error %d: %s", e.code, e.reason)
        return {"error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        log.error("RevenueCat collect failed: %s", e)
        return {"error": str(e)}
