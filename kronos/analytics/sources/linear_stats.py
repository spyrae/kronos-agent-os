"""Linear data source — dev velocity, completed tasks, bugs via REST API.

Note: Linear MCP is available in Claude Code, but Kronos agents use direct
HTTP API. Linear GraphQL API with personal API key.
"""

import json
import logging
import urllib.request
from datetime import datetime, timedelta, timezone

from kronos.config import settings

log = logging.getLogger("kronos.analytics.sources.linear_stats")

_TIMEOUT = 15
_GRAPHQL_URL = "https://api.linear.app/graphql"


def _graphql(query: str, variables: dict | None = None) -> dict:
    """Execute a GraphQL query against Linear API."""
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        _GRAPHQL_URL,
        data=body,
        headers={
            "Authorization": settings.linear_api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    data = json.loads(resp.read())

    if data.get("errors"):
        raise RuntimeError(f"Linear GraphQL: {data['errors'][0].get('message', 'unknown')}")

    return data.get("data", {})


def collect() -> dict:
    """Collect dev velocity metrics for weekly pulse."""
    if not settings.linear_api_key:
        return {"error": "Linear not configured"}

    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).isoformat()

    try:
        # Completed issues this week
        completed_data = _graphql("""
            query($after: DateTimeOrDuration!) {
                issues(
                    filter: {
                        completedAt: { gte: $after }
                        team: { key: { eq: "DEV" } }
                    }
                    first: 250
                ) {
                    nodes { id }
                }
            }
        """, {"after": week_ago})
        completed = len(completed_data.get("issues", {}).get("nodes", []))

        # In Progress issues
        in_progress_data = _graphql("""
            query {
                issues(
                    filter: {
                        state: { type: { eq: "started" } }
                        team: { key: { eq: "DEV" } }
                    }
                    first: 250
                ) {
                    nodes { id }
                }
            }
        """)
        in_progress = len(in_progress_data.get("issues", {}).get("nodes", []))

        # Open bugs
        bugs_data = _graphql("""
            query {
                issues(
                    filter: {
                        labels: { name: { eq: "Bug" } }
                        state: { type: { in: ["backlog", "unstarted", "started"] } }
                        team: { key: { eq: "DEV" } }
                    }
                    first: 250
                ) {
                    nodes { id }
                }
            }
        """)
        bugs_open = len(bugs_data.get("issues", {}).get("nodes", []))

        # Backlog size
        backlog_data = _graphql("""
            query {
                issues(
                    filter: {
                        state: { type: { in: ["backlog", "unstarted"] } }
                        team: { key: { eq: "DEV" } }
                    }
                    first: 250
                ) {
                    nodes { id }
                }
            }
        """)
        backlog = len(backlog_data.get("issues", {}).get("nodes", []))

        return {
            "completed_this_week": completed,
            "in_progress": in_progress,
            "bugs_open": bugs_open,
            "backlog_size": backlog,
        }

    except Exception as e:
        log.error("Linear stats collect failed: %s", e)
        return {"error": str(e)}
