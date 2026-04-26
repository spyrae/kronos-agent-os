"""Supabase data source — user counts, trip stats via PostgREST API."""

import json
import logging
import urllib.parse
import urllib.request

from kronos.config import settings

log = logging.getLogger("kronos.analytics.sources.supabase_stats")

_TIMEOUT = 15


def _rest_get(table: str, params: dict | None = None, head: bool = False) -> dict | list | int:
    """GET request to Supabase PostgREST API.

    Args:
        table: Table name or RPC function.
        params: Query parameters (filters, select, etc.).
        head: If True, use HEAD request to get count only.
    """
    base = settings.supabase_url.rstrip("/")
    url = f"{base}/rest/v1/{table}"
    if params:
        url += "?" + urllib.parse.urlencode(params, safe="(),*:")

    headers = {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "Accept": "application/json",
    }

    if head:
        headers["Prefer"] = "count=exact"
        headers["Range"] = "0-0"

    method = "HEAD" if head else "GET"
    req = urllib.request.Request(url, headers=headers, method=method)
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)

    if head:
        # Count from Content-Range header: "0-0/1234"
        content_range = resp.headers.get("Content-Range", "")
        if "/" in content_range:
            return int(content_range.split("/")[-1])
        return 0

    return json.loads(resp.read())


def _count(table: str, filters: dict | None = None) -> int | None:
    """Get row count for a table with optional filters."""
    try:
        params = {"select": "count"}
        if filters:
            params.update(filters)
        return _rest_get(table, params, head=True)
    except Exception as e:
        log.debug("Count for %s failed: %s", table, e)
        return None


def _rpc(function: str, params: dict | None = None) -> dict | list:
    """Call a Supabase RPC function."""
    base = settings.supabase_url.rstrip("/")
    url = f"{base}/rest/v1/rpc/{function}"

    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "apikey": settings.supabase_service_role_key,
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    return json.loads(resp.read())


def collect() -> dict:
    """Collect Supabase product stats for daily pulse."""
    if not settings.supabase_url or not settings.supabase_service_role_key:
        return {"error": "Supabase not configured"}

    try:
        # Total users
        total_users = _count("global_users")

        # New users in last 24h (using created_at filter)
        new_users = _count("global_users", {
            "created_at": "gte.now()-interval'24 hours'",
        })

        # Active trips (not archived)
        active_trips = _count("trip", {
            "is_archived": "eq.false",
        })

        # Total trips
        total_trips = _count("trip")

        return {
            "total_users": total_users,
            "new_users_24h": new_users,
            "active_trips": active_trips,
            "total_trips": total_trips,
        }

    except Exception as e:
        log.error("Supabase stats collect failed: %s", e)
        return {"error": str(e)}
