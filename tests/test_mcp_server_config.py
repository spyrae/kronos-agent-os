from kronos.config import settings
from kronos.tools.mcp_servers import build_mcp_config


def _set_google_workspace_env(monkeypatch, tmp_path, agent_name: str) -> None:
    monkeypatch.setattr(settings, "agent_name", agent_name)
    monkeypatch.setattr(settings, "workspace_path", str(tmp_path))
    monkeypatch.setattr(settings, "google_oauth_client_id", "client-id")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "client-secret")
    monkeypatch.setenv("GOOGLE_WORKSPACE_MCP_AGENT", "kronos")


def test_google_workspace_mcp_skipped_for_non_owner_agent(monkeypatch, tmp_path):
    _set_google_workspace_env(monkeypatch, tmp_path, agent_name="impulse")

    config = build_mcp_config()

    assert "google-workspace" not in config


def test_google_workspace_mcp_enabled_for_owner_agent(monkeypatch, tmp_path):
    _set_google_workspace_env(monkeypatch, tmp_path, agent_name="kronos")

    config = build_mcp_config()

    assert "google-workspace" in config
