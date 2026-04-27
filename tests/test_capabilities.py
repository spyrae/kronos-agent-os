import pytest

from kronos.config import Settings, settings


def test_empty_telegram_allowlist_blocks_by_default():
    cfg = Settings(_env_file=None, allowed_users="", allow_all_users=False)

    assert cfg.allowed_user_ids == set()
    assert cfg.is_telegram_user_allowed(123) is False
    assert "NONE" in cfg.telegram_access_description


def test_allow_all_users_is_explicit():
    cfg = Settings(_env_file=None, allowed_users="", allow_all_users=True)

    assert cfg.is_telegram_user_allowed(123) is True
    assert "ALLOW_ALL_USERS=true" in cfg.telegram_access_description


def test_allowed_users_take_precedence():
    cfg = Settings(_env_file=None, allowed_users="123, 456", allow_all_users=False)

    assert cfg.allowed_user_ids == {123, 456}
    assert cfg.is_telegram_user_allowed(123) is True
    assert cfg.is_telegram_user_allowed(999) is False


def test_allowed_users_ignores_blank_comment_placeholder():
    cfg = Settings(
        _env_file=None,
        allowed_users="# comma-separated Telegram user IDs",
        allow_all_users=False,
    )

    assert cfg.allowed_user_ids == set()
    assert cfg.invalid_allowed_user_tokens == ()
    assert cfg.is_telegram_user_allowed(123) is False


def test_allowed_users_reports_invalid_tokens():
    cfg = Settings(_env_file=None, allowed_users="123, not-a-user", allow_all_users=False)

    assert cfg.allowed_user_ids == {123}
    assert cfg.invalid_allowed_user_tokens == ("not-a-user",)


@pytest.mark.asyncio
async def test_dynamic_tool_creation_is_disabled_by_default(monkeypatch):
    from kronos.tools.dynamic import create_tool, load_persisted_tools
    from kronos.tools.dynamic_tools import get_dynamic_management_tools

    monkeypatch.setattr(settings, "enable_dynamic_tools", False)

    tool, message = await create_tool("sample_tool", "Return a sample string")

    assert tool is None
    assert "disabled" in message.lower()
    assert load_persisted_tools() == []
    assert get_dynamic_management_tools() == []


@pytest.mark.asyncio
async def test_dynamic_tool_sandbox_fails_closed(monkeypatch):
    from kronos.tools import sandbox

    monkeypatch.setattr(settings, "require_dynamic_tool_sandbox", True)
    monkeypatch.setattr(sandbox, "_docker_available", lambda: False)

    stdout, stderr = await sandbox.execute_sandboxed("print('unsafe')")

    assert stdout == ""
    assert "Docker is required" in stderr
