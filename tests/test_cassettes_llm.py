"""LLM cassettes: keys, record, replay, fail-loud misses (moat phase 8.1)."""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from kronos import cassettes
from kronos.cassettes.store import CassetteStore, llm_key


class FakeTool:
    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description


class FakeModel:
    """Minimal chat model: counts calls so replay can be proven not to call it."""

    model_name = "fake-model-1"

    def __init__(self, response: AIMessage | None = None):
        self.calls = 0
        self.bound_tools: list | None = None
        self._response = response or AIMessage(content="привет")

    def bind_tools(self, tools):
        clone = FakeModel(self._response)
        clone.bound_tools = tools
        clone.calls = self.calls
        return clone

    async def ainvoke(self, messages, *args, **kwargs):
        self.calls += 1
        return self._response

    def invoke(self, messages, *args, **kwargs):
        self.calls += 1
        return self._response


@pytest.fixture
def cassette_env(tmp_path, monkeypatch):
    monkeypatch.setenv(cassettes.ENV_DIR, str(tmp_path / "cassettes"))
    monkeypatch.delenv(cassettes.ENV_MODE, raising=False)
    return tmp_path / "cassettes"


def _messages(text="как дела"):
    return [SystemMessage(content="ты помощник"), HumanMessage(content=text)]


def test_mode_defaults_to_off_and_validates(cassette_env, monkeypatch):
    assert cassettes.mode() == cassettes.MODE_OFF
    assert cassettes.active() is False

    monkeypatch.setenv(cassettes.ENV_MODE, "REPLAY")
    assert cassettes.mode() == cassettes.MODE_REPLAY  # case-insensitive

    monkeypatch.setenv(cassettes.ENV_MODE, "nonsense")
    assert cassettes.mode() == cassettes.MODE_OFF  # unknown value never enables


def test_off_mode_is_fully_transparent(cassette_env):
    model = FakeModel()

    wrapped = cassettes.wrap_model(model, label="standard")

    assert wrapped is model
    assert not cassette_env.exists()


def test_key_changes_with_content_and_tools():
    base = llm_key(messages=_messages(), tools=None, label="standard")

    assert base == llm_key(messages=_messages(), tools=None, label="standard")
    assert base != llm_key(messages=_messages("что нового"), tools=None, label="standard")
    assert base != llm_key(messages=_messages(), tools=[FakeTool("web_search")], label="standard")
    assert base != llm_key(messages=_messages(), tools=None, label="lite")


def test_key_ignores_the_model_name():
    """Replay has no provider, so it cannot know which model answered.

    Keying on the model name would mean replay could never reproduce a key.
    Comparing models is a separate run with its own KAOS_CASSETTE_DIR.
    """
    tools = [FakeTool("web_search", "поиск")]
    with_tools = llm_key(messages=_messages(), tools=tools, label="standard")

    assert with_tools == llm_key(messages=_messages(), tools=tools, label="standard")
    assert with_tools != llm_key(messages=_messages(), tools=None, label="standard")


def test_key_ignores_tool_call_ids():
    """Ids are random per run; if they keyed the cassette nothing would ever hit."""
    first = AIMessage(content="", tool_calls=[{"name": "web_search", "args": {"q": "kaos"}, "id": "call_1"}])
    second = AIMessage(content="", tool_calls=[{"name": "web_search", "args": {"q": "kaos"}, "id": "call_2"}])

    assert llm_key(messages=[first], tools=None, label="l") == llm_key(messages=[second], tools=None, label="l")


def test_key_is_stable_under_pii_masking():
    """A cassette recorded from a real turn must match a scrubbed scenario."""
    from kronos.security.pii import mask_pii

    raw_text = "пиши на roman@example.com"
    raw = llm_key(messages=_messages(raw_text), tools=None, label="l")
    masked = llm_key(messages=_messages(mask_pii(raw_text)), tools=None, label="l")

    assert raw == masked


@pytest.mark.asyncio
async def test_record_then_replay_returns_same_response(cassette_env, monkeypatch):
    response = AIMessage(content="", tool_calls=[{"name": "web_search", "args": {"q": "kaos"}, "id": "call_7"}])
    model = FakeModel(response)

    monkeypatch.setenv(cassettes.ENV_MODE, cassettes.MODE_RECORD)
    recorder = cassettes.wrap_model(model, label="standard").bind_tools([FakeTool("web_search", "поиск")])
    recorded = await recorder.ainvoke(_messages())

    assert recorded is response
    assert CassetteStore(cassette_env).count(cassettes.KIND_LLM) == 1

    monkeypatch.setenv(cassettes.ENV_MODE, cassettes.MODE_REPLAY)
    player = cassettes.replay_model(label="standard").bind_tools([FakeTool("web_search", "поиск")])
    replayed = await player.ainvoke(_messages())

    assert replayed.tool_calls[0]["name"] == "web_search"
    assert replayed.tool_calls[0]["id"] == "call_7"  # ids survive so the turn wires up
    assert replayed.response_metadata.get("cassette") is True


@pytest.mark.asyncio
async def test_replay_never_calls_the_provider(cassette_env, monkeypatch):
    model = FakeModel(AIMessage(content="из провайдера"))
    monkeypatch.setenv(cassettes.ENV_MODE, cassettes.MODE_RECORD)
    await cassettes.wrap_model(model, label="standard").ainvoke(_messages())
    calls_after_record = model.calls

    monkeypatch.setenv(cassettes.ENV_MODE, cassettes.MODE_REPLAY)
    wrapped = cassettes.CassetteChatModel(
        model, label="standard", mode=cassettes.MODE_REPLAY, store=cassettes.get_store()
    )
    replayed = await wrapped.ainvoke(_messages())

    assert replayed.content == "из провайдера"
    assert model.calls == calls_after_record  # not one more


@pytest.mark.asyncio
async def test_replay_miss_fails_loudly(cassette_env, monkeypatch):
    monkeypatch.setenv(cassettes.ENV_MODE, cassettes.MODE_REPLAY)
    player = cassettes.replay_model(label="standard")

    with pytest.raises(cassettes.CassetteMissError, match="no cassette for label=standard"):
        await player.ainvoke(_messages("ничего не записано"))


def test_sync_invoke_records_and_replays(cassette_env, monkeypatch):
    model = FakeModel(AIMessage(content="синхронно"))

    monkeypatch.setenv(cassettes.ENV_MODE, cassettes.MODE_RECORD)
    cassettes.wrap_model(model, label="lite").invoke(_messages())

    monkeypatch.setenv(cassettes.ENV_MODE, cassettes.MODE_REPLAY)
    assert cassettes.replay_model(label="lite").invoke(_messages()).content == "синхронно"


@pytest.mark.asyncio
async def test_recorded_cassette_holds_no_secrets(cassette_env, monkeypatch):
    monkeypatch.setenv(cassettes.ENV_MODE, cassettes.MODE_RECORD)
    model = FakeModel(AIMessage(content="ключ: sk-abcdefghijklmnop1234"))

    await cassettes.wrap_model(model, label="standard").ainvoke(
        [HumanMessage(content="вот токен Bearer abcdefghijklmnop123456 и почта roman@example.com")]
    )

    blob = "\n".join(path.read_text(encoding="utf-8") for path in cassette_env.rglob("*.json"))
    assert "sk-abcdefghijklmnop1234" not in blob
    assert "abcdefghijklmnop123456" not in blob
    assert "roman@example.com" not in blob


def test_tool_cassette_round_trip(cassette_env, monkeypatch):
    monkeypatch.setenv(cassettes.ENV_MODE, cassettes.MODE_RECORD)

    assert cassettes.read_tool_result("web_search", {"q": "kaos"}) is None
    cassettes.write_tool_result("web_search", {"q": "kaos"}, "три результата")

    assert cassettes.read_tool_result("web_search", {"q": "kaos"}) == "три результата"
    assert cassettes.read_tool_result("web_search", {"q": "other"}) is None
    # Stored per tool name, so a cassette directory stays readable.
    assert (cassette_env / cassettes.KIND_TOOL / "web_search").is_dir()


def test_malformed_cassette_is_ignored_not_fatal(cassette_env, monkeypatch):
    store = CassetteStore(cassette_env)
    key = llm_key(messages=_messages(), tools=None, label="standard")
    path = store.path_for(cassettes.KIND_LLM, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")

    monkeypatch.setenv(cassettes.ENV_MODE, cassettes.MODE_REPLAY)
    with pytest.raises(cassettes.CassetteMissError):
        cassettes.replay_model(label="standard").invoke(_messages())


def test_stored_payload_shape_is_documented(cassette_env, monkeypatch):
    monkeypatch.setenv(cassettes.ENV_MODE, cassettes.MODE_RECORD)
    cassettes.wrap_model(FakeModel(AIMessage(content="ответ")), label="standard").invoke(_messages())

    path = next(cassette_env.rglob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert set(payload) >= {"key", "label", "model", "response"}
    assert payload["response"]["content"] == "ответ"
