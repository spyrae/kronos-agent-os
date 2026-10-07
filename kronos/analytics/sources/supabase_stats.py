"""Supabase data source — user counts, trip stats via PostgREST API."""

import json
import logging
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

from kronos.config import settings

log = logging.getLogger("kronos.analytics.sources.supabase_stats")

# PostgREST filters for "not a test account": on global_users itself, on a
# table with a global_user_id foreign key, and on chat messages, which reach
# the user through their chat.
#
# The join names its column because subscriptions is linked to global_users
# twice (its owner, and global_users.current_subscription_id pointing back);
# without the hint PostgREST answers 300 and the count is lost.
_REAL_USERS = {"is_test": "eq.false"}
_OWNED_BY_REAL_USER = {
    "select": "id,global_users!global_user_id!inner(is_test)",
    "global_users.is_test": "eq.false",
}
_IN_REAL_USER_CHAT = {
    "select": "id,ai_chats!inner(global_users!global_user_id!inner(is_test))",
    "ai_chats.global_users.is_test": "eq.false",
}

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
        result = _rest_get(table, params, head=True)
        return result if isinstance(result, int) else None
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


def _test_user_ids() -> set[str] | None:
    """Ids of accounts flagged as test, or None when they cannot be read.

    None keeps DAU unknown rather than letting test activity back in.
    """
    try:
        rows = _rest_get("global_users", {"select": "id", "is_test": "eq.true", "limit": "10000"})
        if not isinstance(rows, list):
            return None
        return {row["id"] for row in rows if row.get("id")}
    except Exception as e:
        log.debug("Test user lookup failed: %s", e)
        return None


def _distinct_users(table: str, column: str, since: str) -> set[str] | None:
    """Distinct user ids that touched `table` since `since`.

    PostgREST cannot do COUNT(DISTINCT), so the ids are fetched and
    de-duplicated here. Returns None — not an empty set — when the request
    fails, so an outage stays distinguishable from a genuinely quiet day.
    """
    try:
        rows = _rest_get(table, {"select": column, "created_at": f"gte.{since}", "limit": "10000"})
        if not isinstance(rows, list):
            return None
        return {row[column] for row in rows if row.get(column)}
    except Exception as e:
        log.debug("Distinct users for %s failed: %s", table, e)
        return None


def collect() -> dict:
    """Collect Supabase product stats for daily pulse."""
    if not settings.supabase_url or not settings.supabase_service_role_key:
        return {"error": "Supabase not configured"}

    try:
        # PostgREST evaluates filter values as literals, not SQL — passing
        # "now()-interval'24 hours'" made the request fail and the field come
        # back null, so the pulse reported no signups regardless of reality.
        since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()

        # Test accounts are left out of every figure. One E2E account owns
        # 130 of 289 trips and a second holds most chat history; with them in,
        # more than half of the reported activity was the test suite.
        total_users = _count("global_users", _REAL_USERS)
        new_users = _count("global_users", {**_REAL_USERS, "created_at": f"gte.{since}"})

        active_trips = _count("trip", {**_OWNED_BY_REAL_USER, "is_archived": "eq.false"})
        total_trips = _count("trip", _OWNED_BY_REAL_USER)

        # Daily activity. These four fields did not exist before: the prompt
        # asks for DAU and key feature usage, the payload carried neither, and
        # the model filled the gap with invented figures every single day.
        trips_24h = _count("trip", {**_OWNED_BY_REAL_USER, "created_at": f"gte.{since}"})
        activities_24h = _count("day_activities", {**_OWNED_BY_REAL_USER, "created_at": f"gte.{since}"})
        ai_messages_24h = _count("ai_chat_messages", {**_IN_REAL_USER_CHAT, "created_at": f"gte.{since}"})
        saved_places_24h = _count("user_poi", {**_OWNED_BY_REAL_USER, "created_at": f"gte.{since}"})

        # DAU spans every table that records a user action, because
        # behavior_events alone has been near-empty since the analytics
        # regression and would understate a working product.
        actors: set[str] = set()
        sources_ok = False
        for table, column in (
            ("trip", "global_user_id"),
            ("day_activities", "global_user_id"),
            ("user_poi", "global_user_id"),
            ("behavior_events", "user_id"),
        ):
            ids = _distinct_users(table, column, since)
            if ids is not None:
                sources_ok = True
                actors |= ids
        test_users = _test_user_ids()
        dau_24h = len(actors - test_users) if sources_ok and test_users is not None else None

        # Subscriptions live here, not in RevenueCat: the RevenueCat source
        # reports 0 active because the records were never mirrored there, and
        # the pulse repeated that zero while the database held active and
        # trialling users.
        active_subscriptions = _count("subscriptions", {**_OWNED_BY_REAL_USER, "status": "eq.active"})
        trial_subscriptions = _count("subscriptions", {**_OWNED_BY_REAL_USER, "status": "eq.trial"})
        # Nothing moves a trial out of "active" when it ends, so the status
        # alone counted 166 trials on a day when 2 were still running.
        now = datetime.now(UTC).isoformat()
        active_trials = _count(
            "user_trials", {**_OWNED_BY_REAL_USER, "status": "eq.active", "ends_at": f"gt.{now}"}
        )

        return {
            "total_users": total_users,
            "new_users_24h": new_users,
            "dau_24h": dau_24h,
            "active_trips": active_trips,
            "total_trips": total_trips,
            "trips_24h": trips_24h,
            "activities_24h": activities_24h,
            "ai_messages_24h": ai_messages_24h,
            "saved_places_24h": saved_places_24h,
            "db_active_subscriptions": active_subscriptions,
            "db_trial_subscriptions": trial_subscriptions,
            "db_active_trials": active_trials,
        }

    except Exception as e:
        log.error("Supabase stats collect failed: %s", e)
        return {"error": str(e)}
