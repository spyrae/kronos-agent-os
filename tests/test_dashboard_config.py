import pytest

from dashboard.api import config


@pytest.mark.asyncio
async def test_capabilities_report_safe_defaults(monkeypatch):
    monkeypatch.setattr(config.settings, "tool_approvals_enabled", True)
    monkeypatch.setattr(config.settings, "enable_dynamic_tools", False)
    monkeypatch.setattr(config.settings, "require_dynamic_tool_sandbox", True)
    monkeypatch.setattr(config.settings, "enable_mcp_gateway_management", False)
    monkeypatch.setattr(config.settings, "enable_dynamic_mcp_servers", False)
    monkeypatch.setattr(config.settings, "enable_server_ops", False)

    result = await config.get_capabilities()
    capabilities = {item["key"]: item for item in result["capabilities"]}

    assert capabilities["tool_approvals"]["status"] == "enabled"
    assert capabilities["tool_approvals"]["risk"] == "protective"
    assert capabilities["tool_approvals"]["required_env"] == "TOOL_APPROVALS_ENABLED=true"
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
    monkeypatch.setattr(config.settings, "tool_approvals_enabled", False)
    monkeypatch.setattr(config.settings, "enable_dynamic_tools", True)
    monkeypatch.setattr(config.settings, "require_dynamic_tool_sandbox", False)
    monkeypatch.setattr(config.settings, "enable_mcp_gateway_management", True)
    monkeypatch.setattr(config.settings, "enable_dynamic_mcp_servers", True)
    monkeypatch.setattr(config.settings, "enable_server_ops", True)

    result = await config.get_capabilities()
    capabilities = {item["key"]: item for item in result["capabilities"]}

    assert capabilities["tool_approvals"]["status"] == "blocked"
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

    created = await config.create_approval(
        config.ApprovalCreate(
            capability="server_ops",
            action="enable",
            reason="need incident diagnostics",
        )
    )
    duplicate = await config.create_approval(
        config.ApprovalCreate(
            capability="server_ops",
            action="enable",
            reason="same request",
        )
    )
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


def test_env_path_is_the_file_the_process_loaded(tmp_path, monkeypatch):
    import kronos.config as kronos_config

    env_file = tmp_path / "kaos.env"
    monkeypatch.setattr(kronos_config, "_ENV_FILE_ABS", str(env_file))

    assert config._get_env_path() == env_file


def test_env_path_does_not_follow_the_working_directory_or_a_late_variable(tmp_path, monkeypatch):
    """Neither a chdir nor an env var set after startup changes which file was loaded."""
    import kronos.config as kronos_config

    loaded = tmp_path / "loaded" / ".env.nexus"
    monkeypatch.setattr(kronos_config, "_ENV_FILE_ABS", str(loaded))
    (tmp_path / ".env").write_text("AGENT_NAME=kaos\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KAOS_ENV_FILE", str(tmp_path / "other.env"))

    assert config._get_env_path() == loaded


@pytest.mark.asyncio
async def test_get_env_vars_masks_secrets(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY=abcd1234efgh5678",
                "AGENT_NAME=kaos",
                "# COMMENT=ignored",
                "",
            ]
        ),
        encoding="utf-8",
    )
    import kronos.config as kronos_config

    monkeypatch.setattr(kronos_config, "_ENV_FILE_ABS", str(env_file))

    result = await config.get_env_vars()
    vars_by_key = {item["key"]: item for item in result["vars"]}

    assert vars_by_key["OPENAI_API_KEY"]["value"] == "abcd********5678"
    assert vars_by_key["OPENAI_API_KEY"]["is_secret"] is True
    assert vars_by_key["AGENT_NAME"]["value"] == "kaos"
    assert vars_by_key["AGENT_NAME"]["is_secret"] is False


# --- F14: what the file says versus what the process runs ---------------------


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """A dotenv file standing in for the one this process loaded."""
    import kronos.config as kronos_config

    path = tmp_path / ".env.nexus"
    path.write_text(
        "TOOL_APPROVALS_ENABLED=false\nENABLE_SERVER_OPS=false\nCUSTOM_FLAG=1\nDEEPSEEK_API_KEY=sk-secret-value-123456\n"
    )
    monkeypatch.setattr(kronos_config, "_ENV_FILE_ABS", str(path))
    for name in ("TOOL_APPROVALS_ENABLED", "ENABLE_SERVER_OPS", "DEEPSEEK_API_KEY", "CUSTOM_FLAG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config.settings, "tool_approvals_enabled", False)
    monkeypatch.setattr(config.settings, "enable_server_ops", False)
    monkeypatch.setattr(config.settings, "deepseek_api_key", "sk-secret-value-123456")
    return path


@pytest.mark.asyncio
async def test_dashboard_edits_the_file_its_own_process_loaded(env_file, tmp_path, monkeypatch):
    """A non-base agent must never read or rewrite the base agent's .env."""
    base = tmp_path / ".env"
    base.write_text("TOOL_APPROVALS_ENABLED=false\n")
    monkeypatch.chdir(tmp_path)

    listing = await config.get_env_vars()
    await config.update_env_var("TOOL_APPROVALS_ENABLED", config.EnvUpdate(value="true"))

    assert listing["env_file"] == ".env.nexus"
    assert base.read_text() == "TOOL_APPROVALS_ENABLED=false\n"
    assert "TOOL_APPROVALS_ENABLED=true" in env_file.read_text()


@pytest.mark.asyncio
async def test_a_saved_value_is_reported_as_not_in_effect_until_restart(env_file):
    before = await config.get_env_vars()
    assert before["restart_required"] is False
    assert {v["key"]: v["applied"] for v in before["vars"]} == {
        "TOOL_APPROVALS_ENABLED": True,
        "ENABLE_SERVER_OPS": True,
        "CUSTOM_FLAG": None,
        "DEEPSEEK_API_KEY": True,
    }

    saved = await config.update_env_var("TOOL_APPROVALS_ENABLED", config.EnvUpdate(value="true"))
    assert saved["applied"] is False and saved["restart_required"] is True
    assert "not in effect" in saved["note"]
    # The process is unchanged, and the API says exactly that.
    assert config.settings.tool_approvals_enabled is False

    after = await config.get_env_vars()
    assert after["restart_required"] is True
    assert {v["key"]: v["applied"] for v in after["vars"]}["TOOL_APPROVALS_ENABLED"] is False


@pytest.mark.asyncio
async def test_saving_the_running_value_or_an_unknown_key_claims_no_more_than_is_known(env_file):
    same = await config.update_env_var("ENABLE_SERVER_OPS", config.EnvUpdate(value="false"))
    assert same["applied"] is True and same["restart_required"] is False

    custom = await config.update_env_var("CUSTOM_FLAG", config.EnvUpdate(value="2"))
    assert custom["applied"] is None and custom["restart_required"] is False
    assert "cannot be told" in custom["note"]


@pytest.mark.asyncio
async def test_comparison_never_exposes_a_secret_value(env_file):
    saved = await config.update_env_var("DEEPSEEK_API_KEY", config.EnvUpdate(value="sk-another-secret-654321"))
    listing = await config.get_env_vars()
    flat = repr(saved) + repr(listing)
    assert saved["applied"] is False
    assert "sk-another-secret-654321" not in flat and "sk-secret-value-123456" not in flat


@pytest.mark.asyncio
async def test_a_protective_capability_enabled_only_in_the_file_is_shown_as_pending(env_file):
    env_file.write_text("TOOL_APPROVALS_ENABLED=true\nENABLE_SERVER_OPS=false\n")

    capabilities = {item["key"]: item for item in (await config.get_capabilities())["capabilities"]}

    approvals = capabilities["tool_approvals"]
    assert approvals["enabled"] is False and approvals["status"] == "blocked", "status describes the running process"
    assert approvals["desired_enabled"] is True and approvals["pending_restart"] is True
    assert capabilities["server_ops"]["pending_restart"] is False


@pytest.mark.asyncio
async def test_a_file_that_would_not_load_is_an_error_not_a_guess(env_file):
    from fastapi import HTTPException

    env_file.write_text("TOOL_APPROVALS_ENABLED=maybe\n")
    with pytest.raises(HTTPException) as failed:
        await config.get_env_vars()
    assert failed.value.status_code == 500


@pytest.mark.asyncio
async def test_process_environment_still_wins_over_the_file(env_file, monkeypatch):
    """A unit-level variable overrides the dotenv, so the file edit changes nothing even after restart."""
    monkeypatch.setenv("TOOL_APPROVALS_ENABLED", "false")
    saved = await config.update_env_var("TOOL_APPROVALS_ENABLED", config.EnvUpdate(value="true"))
    assert saved["applied"] is True and saved["restart_required"] is False
