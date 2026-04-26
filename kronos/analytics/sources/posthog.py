"""PostHog data source — DAU, signups, feature adoption via REST API."""

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from kronos.config import settings

log = logging.getLogger("kronos.analytics.sources.posthog")

_TIMEOUT = 20


def _api_get(path: str, params: dict | None = None) -> dict | list:
    """GET request to PostHog API."""
    host = settings.posthog_host.rstrip("/")
    url = host + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {settings.posthog_api_key}",
            "Accept": "application/json",
        },
    )
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    return json.loads(resp.read())


def _api_post(path: str, body: dict) -> dict:
    """POST request to PostHog API."""
    host = settings.posthog_host.rstrip("/")
    url = host + path

    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {settings.posthog_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    return json.loads(resp.read())


def _event_count(event: str, days: int = 1) -> int | None:
    """Count events over the last N days using PostHog Trends API."""
    project_id = settings.posthog_project_id
    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    date_to = now.strftime("%Y-%m-%d")

    try:
        result = _api_post(f"/api/projects/{project_id}/insights/trend/", {
            "events": [{"id": event, "math": "total"}],
            "date_from": date_from,
            "date_to": date_to,
            "interval": "day",
        })
        # Sum all data points
        data_points = result.get("result", [{}])[0].get("data", [])
        return sum(int(v) for v in data_points)
    except Exception as e:
        log.debug("Event count for %s failed: %s", event, e)
        return None


def _unique_users(event: str, days: int = 1) -> int | None:
    """Count unique users for an event using PostHog Trends API."""
    project_id = settings.posthog_project_id
    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    date_to = now.strftime("%Y-%m-%d")

    try:
        result = _api_post(f"/api/projects/{project_id}/insights/trend/", {
            "events": [{"id": event, "math": "dau"}],
            "date_from": date_from,
            "date_to": date_to,
            "interval": "day",
        })
        # Last data point = today's unique users
        data_points = result.get("result", [{}])[0].get("data", [])
        return int(data_points[-1]) if data_points else None
    except Exception as e:
        log.debug("Unique users for %s failed: %s", event, e)
        return None


def collect() -> dict:
    """Collect product analytics for daily pulse."""
    if not settings.posthog_api_key or not settings.posthog_project_id:
        return {"error": "PostHog not configured"}

    try:
        # DAU — unique users with any event yesterday
        dau = _unique_users("Application Opened", days=1)

        # New signups in last 24h
        signups = _event_count("auth_signup_completed", days=1)

        # Feature adoption (last 24h)
        trips_created = _event_count("trip_created", days=1)
        ai_messages = _event_count("chat_message_sent", days=1)
        places_saved = _event_count("poi_saved", days=1)

        return {
            "dau": dau,
            "new_signups_24h": signups,
            "trips_created_24h": trips_created,
            "ai_messages_24h": ai_messages,
            "places_saved_24h": places_saved,
        }

    except Exception as e:
        log.error("PostHog collect failed: %s", e)
        return {"error": str(e)}
