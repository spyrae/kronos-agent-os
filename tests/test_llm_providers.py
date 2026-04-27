import os
import subprocess
import sys
from types import SimpleNamespace

from kronos.config import settings
from kronos.llm import (
    ModelTier,
    describe_provider_chain,
    get_model,
    is_runtime_llm_configured,
    provider_chain,
    reset_provider_state,
    resolve_provider_config,
)


def _clear_llm_keys(monkeypatch):
    monkeypatch.setattr(settings, "fireworks_api_key", "")
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "groq_api_key", "")
    monkeypatch.setattr(settings, "litellm_admin_key", "")
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
    ]:
        monkeypatch.delenv(name, raising=False)


def test_default_provider_chains_preserve_existing_order(monkeypatch):
    _clear_llm_keys(monkeypatch)
    monkeypatch.setattr(settings, "kaos_standard_provider_chain", "kimi,deepseek")
    monkeypatch.setattr(settings, "kaos_lite_provider_chain", "deepseek,kimi")

    assert provider_chain(ModelTier.STANDARD) == ["kimi", "deepseek"]
    assert provider_chain(ModelTier.LITE) == ["deepseek", "kimi"]


def test_deepseek_key_keeps_default_runtime_configured(monkeypatch):
    _clear_llm_keys(monkeypatch)
    monkeypatch.setattr(settings, "kaos_standard_provider_chain", "kimi,deepseek")
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test")

    rows = describe_provider_chain(ModelTier.STANDARD)

    assert is_runtime_llm_configured() is True
    assert rows[0]["provider"] == "kimi"
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
    assert calls == [{
        "model": "my-model",
        "api_key": "sk-test",
        "max_tokens": 4096,
        "temperature": 0.5,
        "base_url": "https://llm.example.com/v1",
    }]
    reset_provider_state()


def test_provider_config_can_come_from_dotenv_without_settings_fields(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join([
            "KAOS_STANDARD_PROVIDER_CHAIN=my-lab",
            "KAOS_LITE_PROVIDER_CHAIN=my-lab",
            "KAOS_PROVIDER_MY_LAB_MODEL=my-model",
            "KAOS_PROVIDER_MY_LAB_BASE_URL=https://llm.example.com/v1",
            "KAOS_PROVIDER_MY_LAB_API_KEY_ENV=MY_LAB_API_KEY",
            "MY_LAB_API_KEY=sk-test",
        ]),
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
