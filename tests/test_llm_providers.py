import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from kronos.config import settings
from kronos.llm import (
    ModelTier,
    describe_provider_chain,
    get_model,
    get_orchestrator_model,
    is_retriable_llm_error,
    is_runtime_llm_configured,
    provider_chain,
    reset_provider_state,
    resolve_provider_config,
)


def _clear_llm_keys(monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "groq_api_key", "")
    monkeypatch.setattr(settings, "litellm_admin_key", "")
    monkeypatch.setattr(settings, "kaos_orchestrator_provider_chain", "")
    monkeypatch.setattr(settings, "kaos_codex_command", "codex")
    monkeypatch.setattr(settings, "kaos_codex_model", "gpt-5.5")
    monkeypatch.setattr(settings, "kaos_codex_timeout_seconds", 180)
    for name in [
        "FIREWORKS_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "GROQ_API_KEY",
        "TOGETHER_API_KEY",
        "LITELLM_API_KEY",
        "OLLAMA_API_KEY",
        "MY_LAB_API_KEY",
        "KAOS_PROVIDER_CODEX_CLI_COMMAND",
        "KAOS_PROVIDER_CODEX_CLI_MODEL",
        "KAOS_PROVIDER_CODEX_CLI_TIMEOUT_SECONDS",
        "KAOS_PROVIDER_PRIMARY_MODEL",
        "KAOS_PROVIDER_PRIMARY_API_KEY",
        "KAOS_PROVIDER_BACKUP_MODEL",
        "KAOS_PROVIDER_BACKUP_API_KEY",
    ]:
        monkeypatch.delenv(name, raising=False)


def test_default_provider_chains_resolve_to_deepseek(monkeypatch):
    _clear_llm_keys(monkeypatch)

    assert provider_chain(ModelTier.STANDARD) == ["deepseek"]
    assert provider_chain(ModelTier.LITE) == ["deepseek"]


def test_deepseek_key_keeps_partial_chain_runtime_configured(monkeypatch):
    _clear_llm_keys(monkeypatch)
    monkeypatch.setattr(settings, "kaos_standard_provider_chain", "unknown-provider,deepseek")
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test")

    rows = describe_provider_chain(ModelTier.STANDARD)

    assert is_runtime_llm_configured() is True
    assert rows[0]["provider"] == "unknown_provider"
    assert rows[0]["configured"] is False
    assert rows[1]["provider"] == "deepseek"
    assert rows[1]["configured"] is True


def test_openai_compatible_provider_can_be_configured_by_env(monkeypatch):
    _clear_llm_keys(monkeypatch)
    monkeypatch.setattr(settings, "kaos_standard_provider_chain", "my-lab")
    monkeypatch.setenv("KAOS_PROVIDER_MY_LAB_MODEL", "my-model")
    monkeypatch.setenv("KAOS_PROVIDER_MY_LAB_BASE_URL", "https://llm.example.com/v1")
    monkeypatch.setenv("KAOS_PROVIDER_MY_LAB_API_KEY_ENV", "MY_LAB_API_KEY")
    monkeypatch.setenv("MY_LAB_API_KEY", "sk-test")
    monkeypatch.setenv("KAOS_PROVIDER_MY_LAB_MAX_TOKENS", "1234")
    monkeypatch.setenv("KAOS_PROVIDER_MY_LAB_TEMPERATURE", "0.2")

    config = resolve_provider_config("my-lab")

    assert config is not None
    assert config.provider_id == "my_lab"
    assert config.adapter == "openai-compatible"
    assert config.ready is True
    assert config.model == "my-model"
    assert config.base_url == "https://llm.example.com/v1"
    assert config.max_tokens == 1234
    assert config.temperature == 0.2


def test_local_openai_compatible_provider_does_not_require_api_key(monkeypatch):
    _clear_llm_keys(monkeypatch)
    monkeypatch.setattr(settings, "kaos_lite_provider_chain", "ollama")

    config = resolve_provider_config("ollama")

    assert config is not None
    assert config.ready is True
    assert config.api_key_required is False
    assert config.base_url == "http://127.0.0.1:11434/v1"


def test_codex_cli_provider_uses_command_not_api_key(monkeypatch):
    _clear_llm_keys(monkeypatch)
    monkeypatch.setattr("kronos.llm.shutil.which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(settings, "kaos_codex_command", "codex")
    monkeypatch.setattr(settings, "kaos_codex_model", "gpt-5.5")
    monkeypatch.setattr(settings, "kaos_codex_timeout_seconds", 240)

    config = resolve_provider_config("codex-cli")

    assert config is not None
    assert config.adapter == "codex-cli"
    assert config.ready is True
    assert config.api_key_required is False
    assert config.api_key == ""
    assert config.command == "codex"
    assert config.model == "gpt-5.5"
    assert config.timeout_seconds == 240


def test_get_orchestrator_model_uses_separate_chain(monkeypatch):
    _clear_llm_keys(monkeypatch)
    reset_provider_state()
    calls = []

    class FakeCodex:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setitem(sys.modules, "kronos.llm_codex", SimpleNamespace(ChatCodexCLI=FakeCodex))
    monkeypatch.setattr("kronos.llm.shutil.which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(settings, "kaos_orchestrator_provider_chain", "codex-cli")
    monkeypatch.setattr(settings, "kaos_standard_provider_chain", "kimi")
    monkeypatch.setattr(settings, "kaos_codex_command", "codex")
    monkeypatch.setattr(settings, "kaos_codex_model", "gpt-5.5")

    model = get_orchestrator_model()

    assert isinstance(model, FakeCodex)
    assert calls[0]["model_name"] == "gpt-5.5"
    assert calls[0]["command"] == "codex"
    reset_provider_state()


def test_get_model_uses_openai_compatible_adapter(monkeypatch):
    _clear_llm_keys(monkeypatch)
    reset_provider_state()
    calls = []

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setitem(sys.modules, "langchain_openai", SimpleNamespace(ChatOpenAI=FakeChatOpenAI))
    monkeypatch.setattr(settings, "kaos_standard_provider_chain", "my-lab")
    monkeypatch.setenv("KAOS_PROVIDER_MY_LAB_MODEL", "my-model")
    monkeypatch.setenv("KAOS_PROVIDER_MY_LAB_BASE_URL", "https://llm.example.com/v1")
    monkeypatch.setenv("KAOS_PROVIDER_MY_LAB_API_KEY_ENV", "MY_LAB_API_KEY")
    monkeypatch.setenv("MY_LAB_API_KEY", "sk-test")

    model = get_model(ModelTier.STANDARD)

    assert isinstance(model, FakeChatOpenAI)
    assert len(calls) == 1
    kwargs = dict(calls[0])
    # The always-on cost-tracking callback is attached to every model.
    from kronos.security.cost_tracking import CostTrackingCallbackHandler

    callbacks = kwargs.pop("callbacks", [])
    assert any(isinstance(cb, CostTrackingCallbackHandler) for cb in callbacks)
    assert kwargs == {
        "model": "my-model",
        "api_key": "sk-test",
        "max_tokens": 4096,
        "temperature": 0.5,
        "base_url": "https://llm.example.com/v1",
    }
    reset_provider_state()


class FakeHTTPError(Exception):
    def __init__(self, status_code: int, message: str = ""):
        super().__init__(message or f"HTTP {status_code}")
        self.status_code = status_code


@pytest.mark.asyncio
async def test_get_model_falls_back_on_retriable_provider_error(monkeypatch):
    _clear_llm_keys(monkeypatch)
    reset_provider_state()
    attempts = []

    class FakeModel:
        def __init__(self, provider_id: str):
            self.provider_id = provider_id
            self.bound_tools = None

        def bind_tools(self, tools):
            bound = FakeModel(self.provider_id)
            bound.bound_tools = tools
            return bound

        async def ainvoke(self, messages):
            attempts.append((self.provider_id, self.bound_tools))
            if self.provider_id == "primary":
                raise FakeHTTPError(503)
            return AIMessage(content="backup ok")

    monkeypatch.setattr(settings, "kaos_standard_provider_chain", "primary,backup")
    monkeypatch.setenv("KAOS_PROVIDER_PRIMARY_MODEL", "primary-model")
    monkeypatch.setenv("KAOS_PROVIDER_PRIMARY_API_KEY", "sk-primary")
    monkeypatch.setenv("KAOS_PROVIDER_BACKUP_MODEL", "backup-model")
    monkeypatch.setenv("KAOS_PROVIDER_BACKUP_API_KEY", "sk-backup")
    monkeypatch.setattr("kronos.llm._create_model", lambda config: FakeModel(config.provider_id))

    model = get_model(ModelTier.STANDARD).bind_tools(["tool"])
    response = await model.ainvoke([HumanMessage(content="hi")])

    assert response.content == "backup ok"
    assert attempts == [("primary", ["tool"]), ("backup", ["tool"])]
    reset_provider_state()


@pytest.mark.asyncio
async def test_get_model_does_not_retry_non_retriable_provider_error(monkeypatch):
    _clear_llm_keys(monkeypatch)
    reset_provider_state()
    attempts = []

    class FakeModel:
        def __init__(self, provider_id: str):
            self.provider_id = provider_id

        async def ainvoke(self, messages):
            attempts.append(self.provider_id)
            if self.provider_id == "primary":
                raise FakeHTTPError(401)
            return AIMessage(content="should not run")

    monkeypatch.setattr(settings, "kaos_standard_provider_chain", "primary,backup")
    monkeypatch.setenv("KAOS_PROVIDER_PRIMARY_MODEL", "primary-model")
    monkeypatch.setenv("KAOS_PROVIDER_PRIMARY_API_KEY", "sk-primary")
    monkeypatch.setenv("KAOS_PROVIDER_BACKUP_MODEL", "backup-model")
    monkeypatch.setenv("KAOS_PROVIDER_BACKUP_API_KEY", "sk-backup")
    monkeypatch.setattr("kronos.llm._create_model", lambda config: FakeModel(config.provider_id))

    model = get_model(ModelTier.STANDARD)
    with pytest.raises(FakeHTTPError):
        await model.ainvoke([HumanMessage(content="hi")])

    assert attempts == ["primary"]
    reset_provider_state()


def test_retriable_llm_error_classification() -> None:
    assert is_retriable_llm_error(FakeHTTPError(503)) is True
    assert is_retriable_llm_error(FakeHTTPError(429)) is True
    assert is_retriable_llm_error(FakeHTTPError(404, "Model not found, inaccessible, and/or not deployed")) is True
    assert is_retriable_llm_error(FakeHTTPError(404, "Not found")) is False
    assert is_retriable_llm_error(TimeoutError("timed out")) is True
    assert is_retriable_llm_error(FakeHTTPError(401)) is False
    assert is_retriable_llm_error(RuntimeError("blocked by shield")) is False


def test_provider_config_can_come_from_dotenv_without_settings_fields(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "KAOS_STANDARD_PROVIDER_CHAIN=my-lab",
                "KAOS_LITE_PROVIDER_CHAIN=my-lab",
                "KAOS_PROVIDER_MY_LAB_MODEL=my-model",
                "KAOS_PROVIDER_MY_LAB_BASE_URL=https://llm.example.com/v1",
                "KAOS_PROVIDER_MY_LAB_API_KEY_ENV=MY_LAB_API_KEY",
                "MY_LAB_API_KEY=sk-test",
            ]
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    for name in list(env):
        if name.startswith("KAOS_PROVIDER_") or name in {
            "KAOS_ENV_FILE",
            "KRONOS_ENV_FILE",
            "FIREWORKS_API_KEY",
            "DEEPSEEK_API_KEY",
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
            "GROQ_API_KEY",
            "TOGETHER_API_KEY",
            "MY_LAB_API_KEY",
        }:
            env.pop(name, None)
    env["KAOS_ENV_FILE"] = str(env_file)
    repo_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = repo_root + os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else repo_root

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from kronos.llm import ModelTier, describe_provider_chain, is_runtime_llm_configured\n"
                "rows = describe_provider_chain(ModelTier.STANDARD)\n"
                "assert is_runtime_llm_configured() is True\n"
                "assert rows[0]['provider'] == 'my_lab'\n"
                "assert rows[0]['configured'] is True\n"
                "print(rows[0]['base_url'])\n"
            ),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert "https://llm.example.com/v1" in result.stdout
