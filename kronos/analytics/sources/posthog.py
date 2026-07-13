"""PostHog data source — DAU, signups, feature adoption via HogQL Query API.

Uses POST /api/projects/<id>/query/ with HogQL SELECT statements.
Requires Personal API Key (phx_) with scope: query:read.

The legacy /insights/trend/ endpoint was deprecated by PostHog in 2025
and now returns "Legacy insight endpoints are not available for this user".
"""

import json
import logging
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

from kronos.config import settings

log = logging.getLogger("kronos.analytics.sources.posthog")

_TIMEOUT = 20


def _hogql(query: str) -> dict:
    """Execute a HogQL query via PostHog Query API."""
    host = settings.posthog_host.rstrip("/")
    project_id = settings.posthog_project_id
    url = f"{host}/api/projects/{project_id}/query/"

    body = {"query": {"kind": "HogQLQuery", "query": query}}
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


def _scalar(query: str) -> int | None:
    """Run HogQL and return first scalar value (results[0][0]) as int."""
    try:
        d = _hogql(query)
        rows = d.get("results") or []
        if rows and rows[0]:
            v = rows[0][0]
            return int(v) if v is not None else 0
    except urllib.error.HTTPError as e:
        body = e.read()[:200].decode("utf-8", errors="replace")
        log.warning("HogQL HTTP %d: %s", e.code, body)
    except Exception as e:
        log.warning("HogQL query failed: %s", e)
    return None


def collect() -> dict:
    """Collect product analytics for daily pulse via HogQL."""
    if not settings.posthog_api_key or not settings.posthog_project_id:
        return {"error": "PostHog not configured"}

    since = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")

    # DAU — unique persons who triggered Application Opened in the last 24h.
    dau = _scalar(
        f"SELECT count(DISTINCT person_id) FROM events "
        f"WHERE event = 'Application Opened' "
        f"AND timestamp >= toDateTime('{since}')"
    )

    # Signup completions — JourneyBay app emits auth_email_verification_completed
    # after a successful email verification, not auth_signup_completed.
    signups = _scalar(
        f"SELECT count() FROM events "
        f"WHERE event = 'auth_email_verification_completed' "
        f"AND timestamp >= toDateTime('{since}')"
    )

    trips = _scalar(f"SELECT count() FROM events WHERE event = 'trip_created' AND timestamp >= toDateTime('{since}')")

    # AI chat traffic — include both fine-grained chat_message_sent and the
    # broader chat_list_viewed so we capture activity even if the message
    # event is not instrumented yet.
    ai_messages = _scalar(
        f"SELECT count() FROM events "
        f"WHERE event IN ('chat_message_sent', 'chat_list_viewed') "
        f"AND timestamp >= toDateTime('{since}')"
    )

    places = _scalar(f"SELECT count() FROM events WHERE event = 'poi_saved' AND timestamp >= toDateTime('{since}')")

    client_errors = _scalar(
        f"SELECT count() FROM events "
        f"WHERE event IN ('error_occurred', 'network_error') "
        f"AND timestamp >= toDateTime('{since}')"
    )

    return {
        "dau": dau,
        "new_signups_24h": signups,
        "trips_created_24h": trips,
        "ai_messages_24h": ai_messages,
        "places_saved_24h": places,
        "client_errors_24h": client_errors,
    }
