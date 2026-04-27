import pytest

from dashboard.api import config


@pytest.mark.asyncio
async def test_capabilities_report_safe_defaults(monkeypatch):
    monkeypatch.setattr(config.settings, "enable_dynamic_tools", False)
    monkeypatch.setattr(config.settings, "require_dynamic_tool_sandbox", True)
    monkeypatch.setattr(config.settings, "enable_mcp_gateway_management", False)
    monkeypatch.setattr(config.settings, "enable_dynamic_mcp_servers", False)
    monkeypatch.setattr(config.settings, "enable_server_ops", False)

    result = await config.get_capabilities()
    capabilities = {item["key"]: item for item in result["capabilities"]}

    assert capabilities["dynamic_tools"]["status"] == "blocked"
    assert capabilities["dynamic_tools"]["required_env"] == "ENABLE_DYNAMIC_TOOLS=true"
    assert capabilities["dynamic_tool_sandbox"]["status"] == "enabled"
    assert capabilities["dynamic_tool_sandbox"]["risk"] == "protective"
    assert capabilities["mcp_gateway_management"]["status"] == "blocked"
    assert capabilities["dynamic_mcp_servers"]["status"] == "blocked"
    assert capabilities["server_ops"]["status"] == "blocked"
    assert capabilities["server_ops"]["risk"] == "critical"
    assert capabilities["server_ops"]["change_mode"] == "approval_required_restart"
    assert capabilities["server_ops"]["can_request_change"] is True


@pytest.mark.asyncio
async def test_capabilities_reflect_enabled_flags(monkeypatch):
    monkeypatch.setattr(config.settings, "enable_dynamic_tools", True)
    monkeypatch.setattr(config.settings, "require_dynamic_tool_sandbox", False)
    monkeypatch.setattr(config.settings, "enable_mcp_gateway_management", True)
    monkeypatch.setattr(config.settings, "enable_dynamic_mcp_servers", True)
    monkeypatch.setattr(config.settings, "enable_server_ops", True)

    result = await config.get_capabilities()
    capabilities = {item["key"]: item for item in result["capabilities"]}

    assert capabilities["dynamic_tools"]["status"] == "enabled"
    assert capabilities["dynamic_tool_sandbox"]["status"] == "blocked"
    assert capabilities["mcp_gateway_management"]["status"] == "enabled"
    assert capabilities["dynamic_mcp_servers"]["status"] == "enabled"
    assert capabilities["server_ops"]["status"] == "enabled"


@pytest.mark.asyncio
async def test_approval_queue_create_deduplicate_and_deny(tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "db_path", str(tmp_path / "kaos" / "session.db"))
    monkeypatch.setattr(config.settings, "workspace_path", str(tmp_path / "workspace"))
    monkeypatch.setattr(config.settings, "enable_server_ops", False)

    created = await config.create_approval(config.ApprovalCreate(
        capability="server_ops",
        action="enable",
        reason="need incident diagnostics",
    ))
    duplicate = await config.create_approval(config.ApprovalCreate(
        capability="server_ops",
        action="enable",
        reason="same request",
    ))
    queue = await config.get_approvals()

    assert created["approval"]["status"] == "pending"
    assert duplicate["deduplicated"] is True
    assert queue["pending"] == 1
    assert queue["approvals"][0]["required_env"] == "ENABLE_SERVER_OPS=true plus private servers.yaml"

    decided = await config.decide_approval(
        created["approval"]["id"],
        config.ApprovalDecision(decision="denied", reason="demo mode"),
    )
    queue = await config.get_approvals()

    assert decided["approval"]["status"] == "denied"
    assert decided["approval"]["decision_reason"] == "demo mode"
    assert queue["pending"] == 0
    assert queue["recent"][0]["status"] == "denied"


def test_env_path_prefers_explicit_kaos_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / "kaos.env"
    monkeypatch.setenv("KAOS_ENV_FILE", str(env_file))
    monkeypatch.delenv("KRONOS_ENV_FILE", raising=False)

    assert config._get_env_path() == env_file


def test_env_path_uses_cwd_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("AGENT_NAME=kaos\n", encoding="utf-8")
    monkeypatch.delenv("KAOS_ENV_FILE", raising=False)
    monkeypatch.delenv("KRONOS_ENV_FILE", raising=False)
    monkeypatch.chdir(tmp_path)

    assert config._get_env_path() == env_file


@pytest.mark.asyncio
async def test_get_env_vars_masks_secrets(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join([
            "OPENAI_API_KEY=abcd1234efgh5678",
            "AGENT_NAME=kaos",
            "# COMMENT=ignored",
            "",
        ]),
        encoding="utf-8",
    )
    monkeypatch.setenv("KAOS_ENV_FILE", str(env_file))

    result = await config.get_env_vars()
    vars_by_key = {item["key"]: item for item in result["vars"]}

    assert vars_by_key["OPENAI_API_KEY"]["value"] == "abcd********5678"
    assert vars_by_key["OPENAI_API_KEY"]["is_secret"] is True
    assert vars_by_key["AGENT_NAME"]["value"] == "kaos"
    assert vars_by_key["AGENT_NAME"]["is_secret"] is False
