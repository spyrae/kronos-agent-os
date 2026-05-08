import asyncio
import subprocess

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool

from kronos.llm_codex import ChatCodexCLI


@tool
def lookup_city(city: str) -> str:
    """Look up a city."""
    return f"{city}: ok"


def test_codex_cli_plain_response_uses_output_file(monkeypatch, tmp_path):
    def fake_run(args, capture_output, stdin, text, timeout, check):
        output_path = args[args.index("--output-last-message") + 1]
        assert args[:2] == ["codex", "exec"]
        assert "--ignore-rules" in args
        assert "--sandbox" in args
        assert "-m" in args
        assert capture_output is True
        assert stdin == subprocess.DEVNULL
        assert text is True
        assert timeout == 12
        assert check is False
        (tmp_path / "seen.txt").write_text(args[-1], encoding="utf-8")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("Привет")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    model = ChatCodexCLI(model_name="gpt-test", timeout_seconds=12)
    response = model.invoke([HumanMessage(content="Скажи привет")])

    assert response.content == "Привет"
    assert "Conversation:" in (tmp_path / "seen.txt").read_text(encoding="utf-8")


def test_codex_cli_bound_tools_parse_tool_call(monkeypatch):
    def fake_run(args, capture_output, stdin, text, timeout, check):
        assert stdin == subprocess.DEVNULL
        output_path = args[args.index("--output-last-message") + 1]
        with open(output_path, "w", encoding="utf-8") as f:
            f.write('{"tool_calls":[{"name":"lookup_city","args":{"city":"Ubud"}}]}')
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    model = ChatCodexCLI().bind_tools([lookup_city])
    response = model.invoke([HumanMessage(content="Найди город")])

    assert response.content == ""
    assert response.tool_calls[0]["name"] == "lookup_city"
    assert response.tool_calls[0]["args"] == {"city": "Ubud"}
    assert response.tool_calls[0]["id"].startswith("call_")


def test_codex_cli_bound_tools_parse_final(monkeypatch):
    def fake_run(args, capture_output, stdin, text, timeout, check):
        assert stdin == subprocess.DEVNULL
        output_path = args[args.index("--output-last-message") + 1]
        prompt = args[-1]
        assert "Available tools:" in prompt
        assert "tool_result:call_1" in prompt
        with open(output_path, "w", encoding="utf-8") as f:
            f.write('{"final":"Готово"}')
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    model = ChatCodexCLI().bind_tools([lookup_city])
    response = model.invoke([
        HumanMessage(content="Найди город"),
        ToolMessage(content="Ubud: ok", tool_call_id="call_1"),
    ])

    assert response.content == "Готово"
    assert not response.tool_calls


@pytest.mark.asyncio
async def test_codex_cli_async_failure_surfaces_stderr(monkeypatch):
    class FakeProc:
        returncode = 1

        async def communicate(self):
            return b"", b"not logged in"

    async def fake_create_subprocess_exec(*args, stdin, stdout, stderr):
        assert stdin == asyncio.subprocess.DEVNULL
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    model = ChatCodexCLI()

    with pytest.raises(RuntimeError, match="not logged in"):
        await model.ainvoke([HumanMessage(content="hi")])
