"""Tool cassettes in execute_tool: replay without side effects (moat phase 8.2)."""

import pytest
from langchain_core.tools import BaseTool

from kronos import cassettes
from kronos.engine import RAW_TOOL_CONTENT_KEY, execute_tool


class CountingTool(BaseTool):
    """Local tool that records how often it actually ran."""

    name: str = "get_status"
    description: str = "вернуть статус"
    calls: int = 0
    reply: str = "всё хорошо"

    def _run(self, **kwargs) -> str:
        self.calls += 1
        return self.reply


class ExternalTool(BaseTool):
    """Tool whose output comes from outside the process."""

    name: str = "web_search"
    description: str = "поиск в вебе"
    calls: int = 0

    def _run(self, **kwargs) -> str:
        self.calls += 1
        return "результат из сети"


class ExplodingTool(BaseTool):
    name: str = "flaky_fetch"
    description: str = "иногда падает"
    calls: int = 0

    def _run(self, **kwargs) -> str:
        self.calls += 1
        raise RuntimeError("upstream 503")


def _external() -> ExternalTool:
    tool = ExternalTool()
    tool.metadata = {"untrusted_output": True}
    return tool


def _call(args: dict | None = None, call_id: str = "call_1") -> dict:
    return {"id": call_id, "args": args or {}}


@pytest.fixture
def cassette_env(tmp_path, monkeypatch):
    monkeypatch.setenv(cassettes.ENV_DIR, str(tmp_path / "cassettes"))
    monkeypatch.delenv(cassettes.ENV_MODE, raising=False)
    return tmp_path / "cassettes"


@pytest.mark.asyncio
async def test_off_mode_records_nothing(cassette_env):
    tool = CountingTool()

    message = await execute_tool(tool, _call())

    assert message.content == "всё хорошо"
    assert tool.calls == 1
    assert not cassette_env.exists()


@pytest.mark.asyncio
async def test_record_then_replay_skips_the_tool(cassette_env, monkeypatch):
    monkeypatch.setenv(cassettes.ENV_MODE, cassettes.MODE_RECORD)
    recording_tool = CountingTool()
    await execute_tool(recording_tool, _call({"scope": "day"}))
    assert recording_tool.calls == 1

    monkeypatch.setenv(cassettes.ENV_MODE, cassettes.MODE_REPLAY)
    replay_tool = CountingTool()
    replay_tool.reply = "этот ответ не должен появиться"
    message = await execute_tool(replay_tool, _call({"scope": "day"}))

    assert message.content == "всё хорошо"
    assert replay_tool.calls == 0


@pytest.mark.asyncio
async def test_replayed_args_are_matched_exactly(cassette_env, monkeypatch):
    monkeypatch.setenv(cassettes.ENV_MODE, cassettes.MODE_RECORD)
    await execute_tool(CountingTool(), _call({"scope": "day"}))

    monkeypatch.setenv(cassettes.ENV_MODE, cassettes.MODE_REPLAY)
    tool = CountingTool()
    tool.reply = "живой ответ для других аргументов"
    message = await execute_tool(tool, _call({"scope": "week"}))

    # Different args: no cassette, and a local tool is allowed to run for real.
    assert tool.calls == 1
    assert message.content == "живой ответ для других аргументов"


@pytest.mark.asyncio
async def test_external_tool_without_cassette_fails_loudly(cassette_env, monkeypatch):
    monkeypatch.setenv(cassettes.ENV_MODE, cassettes.MODE_REPLAY)
    tool = _external()

    with pytest.raises(cassettes.CassetteMissError, match="no cassette for external tool 'web_search'"):
        await execute_tool(tool, _call({"q": "kaos"}))
    assert tool.calls == 0  # the network is never touched


@pytest.mark.asyncio
async def test_untrusted_framing_is_reapplied_on_replay(cassette_env, monkeypatch):
    monkeypatch.setenv(cassettes.ENV_MODE, cassettes.MODE_RECORD)
    recorded = await execute_tool(_external(), _call({"q": "kaos"}))
    assert "результат из сети" in recorded.content
    assert 'source="tool:web_search"' in recorded.content  # framed as data

    monkeypatch.setenv(cassettes.ENV_MODE, cassettes.MODE_REPLAY)
    tool = _external()
    replayed = await execute_tool(tool, _call({"q": "kaos"}))

    assert tool.calls == 0
    assert "результат из сети" in replayed.content
    assert 'source="tool:web_search"' in replayed.content
    # The untrusted boundary carries a random id by design (an attacker must not
    # be able to close it), so replayed framing is equivalent, not byte-equal.
    # Behaviour comparison therefore has to look at structure, not at prose.
    assert replayed.content != recorded.content
    assert replayed.additional_kwargs[RAW_TOOL_CONTENT_KEY] == "результат из сети"

    # The cassette holds the unwrapped text, so changing the framing later does
    # not invalidate every recorded call.
    stored = cassettes.read_tool_call("web_search", {"q": "kaos"})
    assert stored["content"] == "результат из сети"


@pytest.mark.asyncio
async def test_errors_are_recorded_and_replayed(cassette_env, monkeypatch):
    monkeypatch.setenv(cassettes.ENV_MODE, cassettes.MODE_RECORD)
    failing = ExplodingTool()
    recorded = await execute_tool(failing, _call())
    assert failing.calls == 1
    assert "503" in recorded.content or "[ERROR]" in recorded.content

    monkeypatch.setenv(cassettes.ENV_MODE, cassettes.MODE_REPLAY)
    replay_tool = ExplodingTool()
    replayed = await execute_tool(replay_tool, _call())

    assert replay_tool.calls == 0
    assert replayed.content == recorded.content
    assert cassettes.read_tool_call("flaky_fetch", {})["error"] is True


@pytest.mark.asyncio
async def test_raw_content_survives_a_replay(cassette_env, monkeypatch):
    """A compacted tool keeps its full output in additional_kwargs, replay too."""

    class BulkTool(BaseTool):
        name: str = "query_items"  # 'query' marker triggers output compaction
        description: str = "много элементов"

        def _run(self, **kwargs):
            return [{"id": index, "title": f"элемент {index}"} for index in range(30)]

    monkeypatch.setenv(cassettes.ENV_MODE, cassettes.MODE_RECORD)
    recorded = await execute_tool(BulkTool(), _call())
    assert RAW_TOOL_CONTENT_KEY in recorded.additional_kwargs

    monkeypatch.setenv(cassettes.ENV_MODE, cassettes.MODE_REPLAY)
    replayed = await execute_tool(BulkTool(), _call())

    assert replayed.content == recorded.content
    assert replayed.additional_kwargs[RAW_TOOL_CONTENT_KEY] == recorded.additional_kwargs[RAW_TOOL_CONTENT_KEY]


@pytest.mark.asyncio
async def test_tool_cassettes_hold_no_secrets(cassette_env, monkeypatch):
    class LeakyTool(BaseTool):
        name: str = "read_config"
        description: str = "читает конфиг"

        def _run(self, **kwargs) -> str:
            return "DEEPSEEK_API_KEY=sk-abcdefghijklmnop1234 for roman@example.com"

    monkeypatch.setenv(cassettes.ENV_MODE, cassettes.MODE_RECORD)
    await execute_tool(LeakyTool(), _call({"token": "sk-secret-argument-value"}))

    blob = "\n".join(path.read_text(encoding="utf-8") for path in cassette_env.rglob("*.json"))
    assert "sk-abcdefghijklmnop1234" not in blob
    assert "sk-secret-argument-value" not in blob
    assert "roman@example.com" not in blob
