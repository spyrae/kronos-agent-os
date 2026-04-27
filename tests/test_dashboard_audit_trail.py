import json

import pytest

from dashboard.api import audit_trail


@pytest.mark.asyncio
async def test_tool_calls_endpoint_filters_events(tmp_path, monkeypatch):
    db_dir = tmp_path / "kaos"
    logs_dir = db_dir / "logs"
    logs_dir.mkdir(parents=True)
    monkeypatch.setattr(audit_trail.settings, "db_path", str(db_dir / "session.db"))

    entries = [
        {
            "ts": "2026-04-27T10:00:00+0000",
            "event": "tool_call",
            "status": "called",
            "tool": "search",
            "capability": "research",
            "session_id": "s1",
            "args_summary": "{\"query\":\"kaos\"}",
        },
        {
            "ts": "2026-04-27T10:00:01+0000",
            "event": "tool_result",
            "status": "ok",
            "tool": "search",
            "capability": "research",
            "session_id": "s1",
            "result_summary": "found",
            "duration_ms": 12,
        },
        {
            "ts": "2026-04-27T10:01:00+0000",
            "event": "tool_result",
            "status": "blocked",
            "tool": "mcp_add_server",
            "capability": "mcp",
            "session_id": "s2",
            "result_summary": "Blocked",
        },
    ]
    (logs_dir / "tool_calls.jsonl").write_text(
        "\n".join(json.dumps(entry) for entry in entries),
        encoding="utf-8",
    )

    result = await audit_trail.get_tool_calls(
        session="s1",
        tool="search",
        status="ok",
        capability="research",
        limit=50,
        offset=0,
    )

    assert result["total"] == 1
    assert result["events"][0]["tool"] == "search"
    assert result["events"][0]["status"] == "ok"
    assert result["events"][0]["duration_ms"] == 12
    assert result["counts"]["by_status"] == {"blocked": 1, "ok": 1, "called": 1}
    assert result["filters"]["sessions"] == ["s1", "s2"]
    assert result["filters"]["capabilities"] == ["mcp", "research"]
