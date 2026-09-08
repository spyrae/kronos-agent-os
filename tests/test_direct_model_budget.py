"""Non-factory transports must admit, account and preserve their modality."""

import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from aso import llm as aso_llm
from kronos import llm, vision
from kronos.config import settings
from kronos.security.cost_tracking import estimate_cost_usd, get_cost_callbacks
from kronos.security.direct_model import record_direct_response
from kronos.security.model_budget import ModelBudgetError
from kronos.seo_geo.trackers import llm as geo_llm
from kronos.swarm_store import get_swarm
from tests.test_cost_tracking import cost_env  # noqa: F401
from tests.test_model_budget import models  # noqa: F401


@pytest.fixture
def direct(request, monkeypatch):
    fixture = request.getfixturevalue("models")
    response = {
        "choices": [{"message": {"content": "direct response"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }
    calls, closed = [], []
    behavior = {}

    class Reply:
        def raise_for_status(self):
            pass

        def json(self):
            return response

        def read(self):
            return json.dumps(response).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append("sync")
            if behavior.get("close_failure"):
                raise RuntimeError("synthetic close failure")

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append("async")
            if behavior.get("close_failure"):
                raise RuntimeError("synthetic close failure")

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            if behavior.get("failure"):
                behavior["failure"]()
            return Reply()

    def urlopen(request, **kwargs):
        calls.append((request.full_url, kwargs))
        if behavior.get("failure"):
            behavior["failure"]()
        return Reply()

    monkeypatch.setattr(aso_llm.httpx, "AsyncClient", Client)
    monkeypatch.setattr(geo_llm.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-only-key")
    monkeypatch.setenv("LITELLM_BASE_URL", "https://no-network.invalid")
    monkeypatch.setenv("LITELLM_ADMIN_KEY", "test-only-key")
    monkeypatch.setattr(aso_llm, "PROVIDERS", [aso_llm.PROVIDERS[0], aso_llm.PROVIDERS[2]])
    return SimpleNamespace(models=fixture, calls=calls, closed=closed, response=response, behavior=behavior)


async def test_aso_records_usage_before_returning_text(direct):
    assert await aso_llm.ask("ASO prompt") == "direct response"
    cost = get_swarm().daily_cost()
    assert (cost["requests"], cost["input_tokens"], cost["output_tokens"]) == (1, 100, 20)
    assert cost["cost_usd"] > 0
    assert len(direct.calls) == 1 and direct.closed == ["async"]


async def test_aso_budget_refusal_does_not_fallback(direct):
    get_swarm().add_cost(agent="nexus", cost_usd=6)
    with pytest.raises(ModelBudgetError):
        await aso_llm.ask("ASO prompt")
    assert not direct.calls


async def test_aso_checks_budget_again_after_provider_failure(direct):
    def fail():
        get_swarm().add_cost(agent="another-agent", cost_usd=6)
        raise TimeoutError("synthetic provider timeout")

    direct.behavior["failure"] = fail
    with pytest.raises(ModelBudgetError):
        await aso_llm.ask("ASO prompt")
    assert len(direct.calls) == 1


async def test_aso_degrades_to_configured_factory_lite(direct):
    get_swarm().add_cost(agent="nexus", cost_usd=4.1)
    assert await aso_llm.ask("ASO prompt", model=aso_llm.Model.REASON) == "lite"
    assert not direct.calls
    assert [call[0] for call in direct.models.attempts] == ["lite"]


def test_geo_accounts_the_measured_engine_without_substitution(direct):
    answer = geo_llm.ask_engine(geo_llm.ENGINES[0], "brand question", "test")
    assert answer["error"] is None and answer["answer"] == "direct response"
    assert get_swarm().daily_cost()["requests"] == 1
    assert direct.closed == ["sync"]


@pytest.mark.parametrize("cost", [4.1, 6])
def test_geo_budget_error_is_not_a_fake_measurement_or_lite_call(direct, cost):
    get_swarm().add_cost(agent="nexus", cost_usd=cost)
    answer = geo_llm.ask_engine(geo_llm.ENGINES[0], "brand question", "test")
    assert answer["error"] and answer["answer"] == ""
    assert not direct.calls and not direct.models.attempts


@pytest.mark.parametrize("script_name", ["recall.py", "contact-profiler.py"])
@pytest.mark.parametrize("cost", [0, 4.1, 6])
def test_script_model_paths_share_admission_and_downgrade(direct, script_name, cost):
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / script_name))
    if cost:
        get_swarm().add_cost(agent="nexus", cost_usd=cost)
    if cost == 6:
        with pytest.raises(ModelBudgetError):
            namespace["ask_deepseek"]("script prompt")
        assert not direct.calls and not direct.models.attempts
    elif cost:
        assert namespace["ask_deepseek"]("script prompt") == "lite"
        assert not direct.calls
        assert [call[0] for call in direct.models.attempts] == ["lite"]
    else:
        assert namespace["ask_deepseek"]("script prompt") == "direct response"
        assert get_swarm().daily_cost()["requests"] == 1
        assert direct.closed == ["sync"]


@pytest.fixture
def openai_vision(direct, monkeypatch):
    calls, closed = [], []

    class Client:
        def __init__(self, **kwargs):
            self.responses = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append(True)
            if direct.behavior.get("close_failure"):
                raise RuntimeError("synthetic close failure")

        async def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                output_text=direct.behavior.get("vision_text", "OCR text"),
                usage=SimpleNamespace(input_tokens=500, output_tokens=50),
            )

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=Client))
    monkeypatch.setattr(settings, "openai_api_key", "test-only-key")
    monkeypatch.setattr(settings, "kaos_vision_provider", "openai-api")
    monkeypatch.setattr(settings, "kaos_vision_model", "gpt-5.5")
    return calls, closed


async def test_api_vision_is_not_priced_as_a_free_subscription(direct, openai_vision):
    assert (await vision.analyze_image_bytes(b"fake-image")).text == "OCR text"
    calls, closed = openai_vision
    assert len(calls) == 1 and closed == [True]
    assert get_swarm().daily_cost()["cost_usd"] == estimate_cost_usd("gpt-5.5", 500, 50)
    assert get_swarm().daily_cost()["cost_usd"] > 0


@pytest.mark.parametrize("text", ["", "  "])
async def test_empty_api_vision_is_accounted_but_not_reported_as_success(direct, openai_vision, text):
    direct.behavior["vision_text"] = text
    with pytest.raises(RuntimeError, match="Vision model returned no text"):
        await vision.analyze_image_bytes(b"fake-image")
    assert get_swarm().daily_cost()["requests"] == 1
    assert get_swarm().daily_cost()["cost_usd"] > 0
    assert openai_vision[1] == [True]


@pytest.mark.parametrize("transport", ["aso", "geo", "vision", "recall.py", "contact-profiler.py"])
async def test_response_usage_is_recorded_even_if_client_close_fails(direct, openai_vision, monkeypatch, transport):
    direct.behavior["close_failure"] = True
    monkeypatch.setattr(aso_llm, "PROVIDERS", aso_llm.PROVIDERS[:1])
    if transport == "geo":
        answer = geo_llm.ask_engine(geo_llm.ENGINES[0], "brand question", "test")
        assert answer["answer"] == "" and "synthetic close failure" in answer["error"]
    else:
        with pytest.raises(RuntimeError, match="synthetic close failure"):
            if transport == "aso":
                await aso_llm.ask("ASO prompt")
            elif transport == "vision":
                await vision.analyze_image_bytes(b"fake-image")
            else:
                script = Path(__file__).resolve().parents[1] / "scripts" / transport
                runpy.run_path(str(script))["ask_deepseek"]("script prompt")
    assert get_swarm().daily_cost()["requests"] == 1
    assert get_swarm().daily_cost()["cost_usd"] > 0


@pytest.mark.parametrize("cost", [4.1, 6])
async def test_api_vision_does_not_swap_modality_or_dispatch_when_blocked(direct, openai_vision, cost):
    get_swarm().add_cost(agent="nexus", cost_usd=cost)
    with pytest.raises(ModelBudgetError):
        await vision.analyze_image_bytes(b"fake-image")
    assert not openai_vision[0] and not direct.models.attempts


async def test_codex_vision_uses_explicit_subscription_accounting(direct, monkeypatch):
    monkeypatch.setattr(settings, "kaos_vision_provider", "codex-cli")
    monkeypatch.setattr(vision.shutil, "which", lambda command: "/test-only/codex")
    invoke = AsyncMock(return_value="OCR result")
    monkeypatch.setattr(vision, "run_codex_command", invoke)
    get_swarm().add_cost(agent="nexus", cost_usd=4.1)
    assert (await vision.analyze_image_bytes(b"fake-image")).text == "OCR result"
    assert get_swarm().daily_cost()["requests"] == 2
    assert get_swarm().daily_cost()["cost_usd"] == 4.1
    invoke.assert_awaited_once()


async def test_codex_vision_still_obeys_hard_admission_limit(direct, monkeypatch):
    monkeypatch.setattr(settings, "kaos_vision_provider", "codex-cli")
    monkeypatch.setattr(vision.shutil, "which", lambda command: "/test-only/codex")
    invoke = AsyncMock(return_value="OCR result")
    monkeypatch.setattr(vision, "run_codex_command", invoke)
    get_swarm().add_cost(agent="nexus", cost_usd=6)
    with pytest.raises(ModelBudgetError):
        await vision.analyze_image_bytes(b"fake-image")
    invoke.assert_not_awaited()


def test_missing_usage_is_accounted_as_estimate_not_free_api(direct):
    record_direct_response(model="gpt-5.5", response={}, input_content="input", output_content="output")
    assert get_swarm().daily_cost()["input_tokens"] > 0
    assert get_swarm().daily_cost()["cost_usd"] > 0


@pytest.mark.parametrize("billing,cost_is_zero", [("api", False), ("subscription", True)])
def test_callback_billing_is_from_adapter_even_without_model_metadata(direct, billing, cost_is_zero):
    handler = get_cost_callbacks(model="gpt-5.5", billing=billing)[0]
    handler.on_chat_model_start({}, [[HumanMessage(content="input")]], run_id="test")
    handler.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(content="output"))]]), run_id="test")
    cost = get_swarm().daily_cost()
    assert cost["requests"] == 1
    assert (cost["cost_usd"] == 0) is cost_is_zero


@pytest.mark.parametrize("adapter,billing", [("codex-cli", "subscription"), ("openai-compatible", "api")])
def test_factory_callback_is_bound_to_provider_billing(direct, monkeypatch, adapter, billing):
    monkeypatch.setattr(llm, "_observability_callbacks", lambda: [])
    config = llm.ProviderConfig(provider_id="test", adapter=adapter, model="gpt-5.5")
    callback = llm._runtime_callbacks(config)[0]
    assert callback._model == "gpt-5.5" and callback._billing == billing


async def test_pre_agent_vision_uses_the_same_chat_budget_and_restores_context(direct, openai_vision):
    from kronos.audit import get_tool_audit_context
    from kronos.bridge_media import media_cost_scope
    from kronos.security.cost_guardian import get_guardian

    original = get_tool_audit_context()
    with media_cost_scope(chat_id=771, topic_id=8, user_id=123):
        assert get_tool_audit_context()["session_id"] == "771"
        assert get_tool_audit_context()["thread_id"] == "771:8"
        await vision.analyze_image_bytes(b"fake-image")
    assert get_guardian()._session_costs["771"] > 0
    get_guardian().record_cost("771", 1.1)
    with (
        media_cost_scope(chat_id=771, topic_id=8, user_id=123),
        pytest.raises(ModelBudgetError, match="Session cost limit"),
    ):
        await vision.analyze_image_bytes(b"fake-image")
    assert len(openai_vision[0]) == 1
    assert get_tool_audit_context() == original
