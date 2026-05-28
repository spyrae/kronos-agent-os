from dashboard.api import sandbox as sandbox_api
from kronos.tools.sandbox_platform import PolicyDecision, SandboxRunRequest, record_sandbox_decision


async def test_sandbox_runs_endpoint_reads_audit_log(tmp_path, monkeypatch):
    from kronos.tools import sandbox_platform

    monkeypatch.setattr(sandbox_platform.settings, "db_path", str(tmp_path / "session.db"))
    audit_path = tmp_path / "logs" / "sandbox_runs.jsonl"
    request = SandboxRunRequest(
        tool_name="net_tool",
        session_id="s1",
        network_domains=("api.example.com",),
    )
    record_sandbox_decision(
        request,
        PolicyDecision(False, "blocked by sandbox policy", ("network:api.example.com",)),
        audit_path=audit_path,
    )

    result = await sandbox_api.get_runs(status="all", limit=100)

    assert result["total"] == 1
    assert result["blocked"] == 1
    assert result["runs"][0]["tool_name"] == "net_tool"


async def test_sandbox_status_endpoint_has_platform_posture(monkeypatch):
    monkeypatch.setattr(
        sandbox_api,
        "sandbox_platform_status",
        lambda: {
            "basic_sandbox": {"docker_available": False},
            "platform": {"ready": True, "execution_ready": False, "network_default": "deny"},
        },
    )

    result = await sandbox_api.get_status()

    assert result["platform"]["ready"] is True
    assert result["platform"]["network_default"] == "deny"
