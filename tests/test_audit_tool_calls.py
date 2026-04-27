import json

from kronos import audit


def _configure_audit_path(tmp_path, monkeypatch):
    db_path = tmp_path / "kaos" / "session.db"
    monkeypatch.setattr(audit.settings, "db_path", str(db_path))
    monkeypatch.setattr(audit.settings, "agent_name", "kaos")
    audit._audit_dir = None
    return db_path.parent / "logs" / "tool_calls.jsonl"


def test_tool_event_audit_redacts_secret_args(tmp_path, monkeypatch):
    tool_log = _configure_audit_path(tmp_path, monkeypatch)
    token = audit.set_tool_audit_context(
        agent="kaos",
        thread_id="thread-1",
        user_id="user-1",
        session_id="session-1",
        source_kind="cli",
    )

    try:
        audit.log_tool_event("tool_call", {
            "name": "fetch_url",
            "call_id": "call-1",
            "turn": 1,
            "args": {
                "url": "https://example.com?api_key=super-secret-key",
                "token": "plain-secret",
            },
        })
    finally:
        audit.reset_tool_audit_context(token)

    entry = json.loads(tool_log.read_text(encoding="utf-8").strip())

    assert entry["event"] == "tool_call"
    assert entry["status"] == "called"
    assert entry["capability"] == "research"
    assert entry["session_id"] == "session-1"
    assert "super-secret-key" not in entry["args_summary"]
    assert "plain-secret" not in entry["args_summary"]
    assert "***REDACTED***" in entry["args_summary"]


def test_tool_result_audit_marks_blocked_and_errors(tmp_path, monkeypatch):
    tool_log = _configure_audit_path(tmp_path, monkeypatch)

    audit.log_tool_event("tool_result", {
        "name": "mcp_add_server",
        "call_id": "call-2",
        "ok": False,
        "content": "Blocked: dynamic MCP server management is disabled.",
        "duration_ms": 7,
    })

    entry = json.loads(tool_log.read_text(encoding="utf-8").strip())

    assert entry["event"] == "tool_result"
    assert entry["status"] == "blocked"
    assert entry["approval_status"] == "blocked"
    assert entry["capability"] == "mcp"
    assert entry["error"] is True
    assert entry["duration_ms"] == 7
