"""Sentry data source — errors, issues, crash rate via REST API."""

import json
import logging
import urllib.parse
import urllib.request

from kronos.config import settings

log = logging.getLogger("kronos.analytics.sources.sentry")

_TIMEOUT = 15
_BASE_URL = "https://sentry.io/api/0"


def _api_get(path: str, params: dict | None = None) -> dict | list:
    """GET request to Sentry API."""
    url = _BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {settings.sentry_auth_token}",
            "Accept": "application/json",
        },
    )
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    return json.loads(resp.read())


def collect() -> dict:
    """Collect error metrics for daily pulse."""
    if not settings.sentry_auth_token:
        return {"error": "Sentry not configured"}

    org = settings.sentry_org
    project = settings.sentry_project

    try:
        # Full unresolved backlog (all-time, capped) — for context, NOT for alerting
        backlog = _api_get(
            f"/projects/{org}/{project}/issues/",
            {"query": "is:unresolved", "sort": "date", "limit": "25"},
        )

        # Unresolved issues that were actually seen in the last 24h — what matters today
        active = _api_get(
            f"/projects/{org}/{project}/issues/",
            {"query": "is:unresolved lastSeen:-24h", "sort": "freq", "limit": "10"},
        )

        # Project stats (events received in last 24h)
        stats = _api_get(
            f"/projects/{org}/{project}/stats/",
            {"stat": "received", "resolution": "1d"},
        )
        events_24h = stats[-1][1] if stats else 0

        # Top issues limited to those ACTIVE in the last 24h (a 9-day-dead issue
        # with a large all-time count must not surface as a fresh spike).
        top_issues = []
        for issue in (active or [])[:5]:
            top_issues.append(
                {
                    "title": issue.get("title", "Unknown")[:100],
                    "total_events_all_time": issue.get("count", "0"),
                    "level": issue.get("level", "error"),
                    "first_seen": issue.get("firstSeen", ""),
                    "last_seen": issue.get("lastSeen", ""),
                }
            )

        return {
            "unresolved_total": len(backlog) if backlog else 0,
            "unresolved_active_24h": len(active) if active else 0,
            "events_24h": events_24h,
            "top_issues_active_24h": top_issues,
        }

    except Exception as e:
        log.error("Sentry collect failed: %s", e)
        return {"error": str(e)}
