"""Zabbix data source — host status, problems, triggers via JSON-RPC API."""

import json
import logging
import urllib.request

from kronos.config import settings

log = logging.getLogger("kronos.analytics.sources.zabbix")

_TIMEOUT = 15


def _api_call(method: str, params: dict | None = None) -> list | dict:
    """Call Zabbix JSON-RPC API."""
    url = settings.zabbix_url.rstrip("/") + "/api_jsonrpc.php"

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": 1,
            "auth": settings.zabbix_token,
        }
    ).encode()

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json-rpc"},
    )
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    data = json.loads(resp.read())

    if "error" in data:
        raise RuntimeError(f"Zabbix API error: {data['error']}")

    return data.get("result", [])


def collect() -> dict:
    """Collect infra health metrics for daily pulse."""
    if not settings.zabbix_url or not settings.zabbix_token:
        return {"error": "Zabbix not configured"}

    try:
        # Host status
        hosts = _api_call(
            "host.get",
            {
                "output": ["host", "name", "status"],
                "selectInterfaces": ["ip"],
                "filter": {"status": "0"},  # only enabled hosts
            },
        )

        # Active problems (severity >= Warning)
        problems = _api_call(
            "problem.get",
            {
                "recent": True,
                "sortfield": ["eventid"],
                "sortorder": "DESC",
                "limit": 20,
                "severities": [2, 3, 4, 5],  # Warning, Average, High, Disaster
                "selectHosts": ["host"],
                "suppressed": False,  # exclude problems intentionally suppressed in Zabbix
            },
        )

        # Active triggers
        triggers = _api_call(
            "trigger.get",
            {
                "only_true": True,
                "min_severity": 2,
                "output": ["description", "priority", "lastchange"],
                "selectHosts": ["host"],
                "limit": 20,
            },
        )

        hosts_total = len(hosts)

        # "Update available" notifications (diun) are informational only — pending
        # Docker image updates, not a health issue. They must NOT influence the
        # assessment (counts / summary). Split them out; report count separately.
        def _is_update(text: str) -> bool:
            t = (text or "").lower()
            return "обновлени" in t or "update available" in t

        real_problems = [p for p in problems if not _is_update(p.get("name", ""))]
        updates_available = [p for p in problems if _is_update(p.get("name", ""))]
        real_triggers = [t for t in triggers if not _is_update(t.get("description", ""))]

        critical_triggers = [t for t in real_triggers if int(t.get("priority", "0")) >= 4]
        warning_triggers = [t for t in real_triggers if int(t.get("priority", "0")) in (2, 3)]

        problems_summary = []
        for p in real_problems[:5]:
            host = p.get("hosts", [{}])[0].get("host", "unknown")
            name = p.get("name", "Unknown problem")
            problems_summary.append(f"{name} on {host}")

        return {
            "hosts_total": hosts_total,
            "active_problems": len(real_problems),
            "critical_triggers": len(critical_triggers),
            "warning_triggers": len(warning_triggers),
            "problems_summary": problems_summary,
            "updates_available": len(updates_available),  # informational only, not a health issue
        }

    except Exception as e:
        log.error("Zabbix collect failed: %s", e)
        return {"error": str(e)}
