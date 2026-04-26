"""Web analytics data source — Yandex Metrika + GA4."""

import json
import logging
import urllib.parse
import urllib.request

from kronos.config import settings

log = logging.getLogger("kronos.analytics.sources.web_analytics")

_TIMEOUT = 15

# ── Yandex Metrika ──────────────────────────────────────────────────


def _ym_api_get(path: str, params: dict) -> dict:
    """GET request to Yandex Metrika API."""
    url = "https://api-metrica.yandex.net" + path
    url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"OAuth {settings.ym_oauth_token}",
            "Accept": "application/json",
        },
    )
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    return json.loads(resp.read())


def _collect_ym() -> dict:
    """Collect Yandex Metrika stats for yesterday."""
    if not settings.ym_oauth_token or not settings.ym_counter_id:
        return {"error": "Yandex Metrika not configured"}

    try:
        data = _ym_api_get("/stat/v1/data", {
            "ids": settings.ym_counter_id,
            "metrics": "ym:s:visits,ym:s:users,ym:s:bounceRate,ym:s:pageviews",
            "date1": "yesterday",
            "date2": "yesterday",
        })

        totals = data.get("totals", [])
        if isinstance(totals, list) and totals and isinstance(totals[0], list):
            totals = totals[0]  # nested format
        if len(totals) >= 4:
            return {
                "ym_visits": int(totals[0]),
                "ym_users": int(totals[1]),
                "ym_bounce_rate": round(totals[2], 1),
                "ym_pageviews": int(totals[3]),
            }
        return {"error": "Unexpected YM response format"}

    except Exception as e:
        log.debug("Yandex Metrika collect failed: %s", e)
        return {"error": str(e)}


# ── Google Analytics 4 ──────────────────────────────────────────────


def _collect_ga4() -> dict:
    """Collect GA4 stats for yesterday.

    Uses Google Analytics Data API v1 with service account credentials.
    Requires GOOGLE_APPLICATION_CREDENTIALS env var pointing to the
    service account JSON file, and ga4_property_id in settings.
    """
    if not settings.ga4_property_id:
        return {"error": "GA4 not configured"}

    try:
        # Use google-auth + google-analytics-data library if available
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import (
            DateRange,
            Metric,
            RunReportRequest,
        )

        client = BetaAnalyticsDataClient()
        request = RunReportRequest(
            property=f"properties/{settings.ga4_property_id}",
            date_ranges=[DateRange(start_date="yesterday", end_date="yesterday")],
            metrics=[
                Metric(name="sessions"),
                Metric(name="newUsers"),
                Metric(name="bounceRate"),
                Metric(name="activeUsers"),
            ],
        )
        response = client.run_report(request)

        if response.rows:
            row = response.rows[0]
            values = [v.value for v in row.metric_values]
            return {
                "ga_sessions": int(values[0]) if values[0] else 0,
                "ga_new_users": int(values[1]) if values[1] else 0,
                "ga_bounce_rate": round(float(values[2]) * 100, 1) if values[2] else None,
                "ga_active_users": int(values[3]) if values[3] else 0,
            }
        return {"error": "No GA4 data for yesterday"}

    except ImportError:
        return {"error": "google-analytics-data package not installed"}
    except Exception as e:
        log.debug("GA4 collect failed: %s", e)
        return {"error": str(e)}


# ── Public API ──────────────────────────────────────────────────────


def collect() -> dict:
    """Collect web analytics for daily pulse.

    Merges data from Yandex Metrika and GA4. Each source handles its
    own errors independently — partial data is fine.
    """
    result = {}

    ym = _collect_ym()
    ga = _collect_ga4()

    ym_ok = "error" not in ym
    ga_ok = "error" not in ga

    # Merge successful data (prefixed keys prevent collisions)
    if ym_ok:
        result.update(ym)
    if ga_ok:
        result.update(ga)

    # If both failed entirely, report combined error
    if not ym_ok and not ga_ok:
        return {"error": f"YM: {ym['error']}; GA4: {ga['error']}"}

    # If one failed, note it as partial
    if not ym_ok:
        result["ym_error"] = ym["error"]
    if not ga_ok:
        result["ga_error"] = ga["error"]

    return result
