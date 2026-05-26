"""Google Search Console tracker — organic clicks + impressions.

Uses the same service-account credentials we already mounted for GA4
(``GOOGLE_APPLICATION_CREDENTIALS=/opt/.../ga4-service-account.json``).
The service account must be added as a USER on each GSC property.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

log = logging.getLogger("kronos.seo_geo.trackers.gsc")


def _client():
    """Build a GSC API client. Returns None if creds are missing or libs absent."""
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds_path or not os.path.exists(creds_path):
        log.debug("GSC: GOOGLE_APPLICATION_CREDENTIALS not set or missing")
        return None
    try:
        from google.oauth2 import service_account  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
    except ImportError:
        log.warning(
            "GSC: install google-api-python-client + google-auth: "
            "pip install google-api-python-client google-auth"
        )
        return None
    scopes = ["https://www.googleapis.com/auth/webmasters.readonly"]
    creds = service_account.Credentials.from_service_account_file(creds_path, scopes=scopes)
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def top_queries(site_url: str, days: int = 28, limit: int = 100) -> list[dict]:
    """Return top queries by impressions for the given GSC site.

    site_url: 'sc-domain:journeybay.co' or 'https://example.com/'.
    Each row: {query, clicks, impressions, ctr, position}.
    """
    svc = _client()
    if svc is None:
        return []
    end = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%d")  # GSC has 3-day lag
    start = (datetime.now(UTC) - timedelta(days=days + 3)).strftime("%Y-%m-%d")

    body = {
        "startDate": start,
        "endDate": end,
        "dimensions": ["query"],
        "rowLimit": limit,
        "orderBy": [{"fieldName": "impressions", "descending": True}],
    }
    try:
        resp = svc.searchanalytics().query(siteUrl=site_url, body=body).execute()
    except Exception as e:
        log.warning("GSC query failed for %s: %s", site_url, e)
        return []

    rows = resp.get("rows", []) or []
    return [
        {
            "query": r["keys"][0],
            "clicks": r.get("clicks", 0),
            "impressions": r.get("impressions", 0),
            "ctr": round(r.get("ctr", 0.0), 4),
            "position": round(r.get("position", 0.0), 1),
        }
        for r in rows
    ]


def total_metrics(site_url: str, days: int = 28) -> dict:
    """Return totals: {clicks, impressions, avg_ctr, avg_position}."""
    svc = _client()
    if svc is None:
        return {}
    end = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%d")
    start = (datetime.now(UTC) - timedelta(days=days + 3)).strftime("%Y-%m-%d")

    body = {
        "startDate": start,
        "endDate": end,
        "dimensions": [],  # no grouping = totals
    }
    try:
        resp = svc.searchanalytics().query(siteUrl=site_url, body=body).execute()
    except Exception as e:
        log.warning("GSC totals failed for %s: %s", site_url, e)
        return {}
    rows = resp.get("rows", []) or []
    if not rows:
        return {"clicks": 0, "impressions": 0, "avg_ctr": 0.0, "avg_position": 0.0}
    r = rows[0]
    return {
        "clicks": r.get("clicks", 0),
        "impressions": r.get("impressions", 0),
        "avg_ctr": round(r.get("ctr", 0.0), 4),
        "avg_position": round(r.get("position", 0.0), 1),
    }
