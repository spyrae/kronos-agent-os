"""Whisper dispatch, duration accounting and pre-agent voice cleanup."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from kronos import bridge, bridge_media
from kronos.audit import get_tool_audit_context
from kronos.config import settings
from kronos.execution_control import ExecutionStoppedError, execution_scope
from kronos.security.cost_guardian import get_guardian
from kronos.security.model_budget import ModelBudgetError, model_budget_scope
from kronos.swarm_store import get_swarm
from tests.test_cost_tracking import cost_env  # noqa: F401
from tests.test_model_budget import models  # noqa: F401
from tests.test_observer_bridge_capture import FakeEvent, FakeMessage, _noop, _registered_message_handler


@pytest.fixture
def speech(request, tmp_path, monkeypatch):
    request.getfixturevalue("models")
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"fake audio; network transport is replaced")
    state = SimpleNamespace(
        audio=str(audio),
        result={"duration": 60.0, "text": " transcript "},
        calls=[],
        files=[],
        closed=[],
        clients=0,
        status=200,
        close_error=None,
        read_error=None,
        reading=asyncio.Event(),
        wait=False,
    )

    class Form:
        def __init__(self):
            self.fields = {}

        def add_field(self, name, value, **kwargs):
            self.fields[name] = value
            if name == "file":
                state.files.append(value)

    class Response:
        @property
        def status(self):
            return state.status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            state.closed.append("response")
            if state.close_error:
                raise state.close_error

        async def text(self):
            return "secret transcript / token test-only-private-value"

        async def json(self):
            state.reading.set()
            if state.wait:
                await asyncio.Event().wait()
            if state.read_error:
                raise state.read_error
            return state.result

    class Client:
        def __init__(self):
            state.clients += 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            state.closed.append("client")

        def post(self, url, **kwargs):
            state.calls.append((url, kwargs))
            return Response()

    monkeypatch.setattr(settings, "groq_api_key", "test-only-key")
    monkeypatch.setattr(bridge_media.aiohttp, "ClientSession", Client)
    monkeypatch.setattr(bridge_media.aiohttp, "FormData", Form)
    return state


@pytest.mark.parametrize("seconds", [0.01, 5, 10, 10.25, 60, 3600])
async def test_voice_accounts_audio_seconds_not_transcript_tokens(speech, seconds):
    speech.result["duration"] = seconds
    original = get_tool_audit_context()
    with bridge_media.media_cost_scope(chat_id=42, topic_id=8, user_id=7):
        assert await bridge_media._transcribe_voice(speech.audio) == "transcript"
    expected = max(10, seconds) / 3600 * 0.04
    cost = get_swarm().daily_cost()
    assert cost["cost_usd"] == round(expected, 6)  # The reporting API rounds; storage must not.
    stored = get_swarm()._db.read_one("SELECT cost_usd FROM swarm_costs WHERE agent = ?", ("nexus",))
    assert stored["cost_usd"] == pytest.approx(expected)
    assert (cost["requests"], cost["input_tokens"], cost["output_tokens"]) == (1, 0, 0)
    assert get_guardian()._session_costs["42"] == pytest.approx(expected)
    assert get_tool_audit_context() == original
    fields = speech.calls[0][1]["data"].fields
    assert fields["response_format"] == "verbose_json"
    assert fields["model"] == "whisper-large-v3-turbo"
    assert speech.closed == ["response", "client"] and speech.files[0].closed


@pytest.mark.parametrize("scope", ["daily", "session", "unavailable"])
async def test_voice_admission_refusal_happens_before_file_or_http(speech, monkeypatch, scope):
    if scope == "daily":
        get_swarm().add_cost(agent="nexus", cost_usd=6)
    elif scope == "session":
        get_guardian().record_cost("42", 1.1)
    else:
        def fail():
            raise OSError("synthetic unavailable ledger")
        monkeypatch.setattr(get_swarm(), "daily_cost", fail)
    with bridge_media.media_cost_scope(chat_id=42, topic_id=None, user_id=7):
        with pytest.raises(ModelBudgetError):
            await bridge_media._transcribe_voice(speech.audio)
    assert not speech.calls and not speech.clients and not speech.files


async def test_voice_turbo_remains_audio_capable_under_soft_downgrade(speech):
    get_swarm().add_cost(agent="nexus", cost_usd=4.1)
    with model_budget_scope("lite"):
        assert await bridge_media._transcribe_voice(speech.audio) == "transcript"
    assert len(speech.calls) == 1
    assert get_swarm().daily_cost()["cost_usd"] > 4.1


@pytest.mark.parametrize("duration", [None, "60", True, -1, 0, float("nan"), float("inf")])
async def test_unknown_audio_usage_is_an_explicit_failure_not_success_or_free_call(speech, duration):
    speech.result["duration"] = duration
    with pytest.raises(RuntimeError, match="audio duration.*cost is unknown"):
        await bridge_media._transcribe_voice(speech.audio)
    # No fabricated zero-price record. Durable unknown-outcome reconciliation is
    # a separate unfinished F12 requirement; this test does not prove its cost.
    assert get_swarm().daily_cost()["requests"] == 0
    assert speech.files[0].closed and speech.closed == ["response", "client"]


@pytest.mark.parametrize("text", ["", "  ", None, 12])
async def test_voice_usage_is_accounted_even_when_transcript_is_invalid(speech, text):
    speech.result["text"] = text
    with pytest.raises(RuntimeError, match="transcription returned no text"):
        await bridge_media._transcribe_voice(speech.audio)
    assert get_swarm().daily_cost()["requests"] == 1
    assert get_swarm().daily_cost()["cost_usd"] > 0
    assert speech.files[0].closed


async def test_voice_close_failure_cannot_erase_received_usage(speech):
    speech.close_error = RuntimeError("synthetic close failure")
    with pytest.raises(RuntimeError, match="synthetic close failure"):
        await bridge_media._transcribe_voice(speech.audio)
    assert get_swarm().daily_cost()["requests"] == 1
    assert speech.files[0].closed and speech.closed == ["response", "client"]


@pytest.mark.parametrize("status", [401, 429, 500])
async def test_voice_http_errors_do_not_expose_provider_body_or_retry(speech, status):
    speech.status = status
    with pytest.raises(RuntimeError, match=f"Groq STT error {status}") as error:
        await bridge_media._transcribe_voice(speech.audio)
    assert "test-only-private-value" not in str(error.value)
    assert len(speech.calls) == 1 and speech.files[0].closed


async def test_voice_cancellation_closes_input_and_http_contexts(speech):
    speech.wait = True
    task = asyncio.create_task(bridge_media._transcribe_voice(speech.audio))
    await asyncio.wait_for(speech.reading.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert speech.files[0].closed and speech.closed == ["response", "client"]
    assert len(speech.calls) == 1


@pytest.mark.parametrize("when", ["before", "after"])
async def test_voice_obeys_execution_stop_and_accounts_a_received_response(speech, when):
    def check():
        if when == "before" or speech.reading.is_set():
            raise ExecutionStoppedError("synthetic execution stopped")

    with execution_scope(check), pytest.raises(ExecutionStoppedError):
        await bridge_media._transcribe_voice(speech.audio)
    assert len(speech.calls) == (0 if when == "before" else 1)
    assert get_swarm().daily_cost()["requests"] == (0 if when == "before" else 1)


@pytest.mark.parametrize("outcome", ["cancel", "budget", "provider_error"])
async def test_bridge_voice_always_removes_temp_and_explains_failure(monkeypatch, outcome, caplog):
    client, handle = await _registered_message_handler(monkeypatch)
    monkeypatch.setattr(bridge, "_human_typing_delay", _noop)
    downloaded = []

    class Message(FakeMessage):
        async def download_media(self, file):
            downloaded.append(Path(file))
            await super().download_media(file)

    async def fail(file_path):
        if outcome == "cancel":
            raise asyncio.CancelledError()
        if outcome == "budget":
            raise ModelBudgetError("synthetic daily budget limit")
        raise RuntimeError("test-only-private-transcript")

    monkeypatch.setattr(settings, "groq_api_key", "test-only-key")
    monkeypatch.setattr(bridge, "_is_voice_message", lambda event: True)
    monkeypatch.setattr(bridge, "_is_image_message", lambda event: False)
    monkeypatch.setattr(bridge, "_transcribe_voice", fail)
    event = FakeEvent(text="", message=Message(media=object()))
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await handle(event)
        assert not client.sent
    else:
        await handle(event)
        assert len(client.sent) == 1
        assert ("бюджет" in client.sent[0]["text"]) is (outcome == "budget")
    assert downloaded and not downloaded[0].exists()
    assert "test-only-private-transcript" not in caplog.text
