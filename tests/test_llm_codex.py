import asyncio
import os
import subprocess

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool

from kronos.llm_codex import ChatCodexCLI


class FinishedProcess:
    returncode = 0

    def __init__(self, timeout):
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def communicate(self, *, timeout):
        assert timeout == self.timeout
        return "", ""


@tool
def lookup_city(city: str) -> str:
    """Look up a city."""
    return f"{city}: ok"


def test_codex_cli_plain_response_uses_output_file(monkeypatch, tmp_path):
    def fake_popen(args, stdin, stdout, stderr, text, start_new_session):
        output_path = args[args.index("--output-last-message") + 1]
        assert args[:2] == ["codex", "exec"]
        assert "--ignore-rules" in args
        assert "--sandbox" in args
        assert "-m" in args
        assert stdout == stderr == subprocess.PIPE
        assert stdin == subprocess.DEVNULL
        assert text is True
        assert start_new_session == (os.name == "posix")
        (tmp_path / "seen.txt").write_text(args[-1], encoding="utf-8")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("Привет")
        return FinishedProcess(timeout=12)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    model = ChatCodexCLI(model_name="gpt-test", timeout_seconds=12)
    response = model.invoke([HumanMessage(content="Скажи привет")])

    assert response.content == "Привет"
    prompt = (tmp_path / "seen.txt").read_text(encoding="utf-8")
    assert "Conversation:" in prompt
    assert "model `gpt-test`" in prompt


def test_codex_cli_bound_tools_parse_tool_call(monkeypatch):
    def fake_popen(args, stdin, stdout, stderr, text, start_new_session):
        assert stdin == subprocess.DEVNULL
        output_path = args[args.index("--output-last-message") + 1]
        with open(output_path, "w", encoding="utf-8") as f:
            f.write('{"tool_calls":[{"name":"lookup_city","args":{"city":"Ubud"}}]}')
        return FinishedProcess(timeout=180)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    model = ChatCodexCLI().bind_tools([lookup_city])
    response = model.invoke([HumanMessage(content="Найди город")])

    assert response.content == ""
    assert response.tool_calls[0]["name"] == "lookup_city"
    assert response.tool_calls[0]["args"] == {"city": "Ubud"}
    assert response.tool_calls[0]["id"].startswith("call_")


def test_codex_cli_bound_tools_parse_final(monkeypatch):
    def fake_popen(args, stdin, stdout, stderr, text, start_new_session):
        assert stdin == subprocess.DEVNULL
        output_path = args[args.index("--output-last-message") + 1]
        prompt = args[-1]
        assert "Available tools:" in prompt
        assert "tool_result:call_1" in prompt
        with open(output_path, "w", encoding="utf-8") as f:
            f.write('{"final":"Готово"}')
        return FinishedProcess(timeout=180)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    model = ChatCodexCLI().bind_tools([lookup_city])
    response = model.invoke(
        [
            HumanMessage(content="Найди город"),
            ToolMessage(content="Ubud: ok", tool_call_id="call_1"),
        ]
    )

    assert response.content == "Готово"
    assert not response.tool_calls


@pytest.mark.asyncio
async def test_codex_cli_async_failure_surfaces_stderr(monkeypatch):
    class FakeProc:
        returncode = 1
        pid = 12345

        async def communicate(self):
            return b"", b"not logged in"

        async def wait(self):
            return self.returncode

    async def fake_create_subprocess_exec(*args, stdin, stdout, stderr, start_new_session):
        assert stdin == asyncio.subprocess.DEVNULL
        assert start_new_session == (os.name == "posix")
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr("kronos.llm_codex._signal_process_tree", lambda *args, **kwargs: None)

    model = ChatCodexCLI()

    with pytest.raises(RuntimeError, match="not logged in"):
        await model.ainvoke([HumanMessage(content="hi")])
